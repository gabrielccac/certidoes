"""Small CDP transport for the frozen Receita flow; no SeleniumBase runtime."""
import asyncio
import base64
from collections import Counter, defaultdict
import inspect
import json
from pathlib import Path
import random
import time
from urllib.request import urlopen

import mycdp
from mycdp.util import parse_json_event
import websocket

EVENT_METHODS = {
    mycdp.network.RequestWillBeSent: "Network.requestWillBeSent",
    mycdp.network.ResponseReceived: "Network.responseReceived",
    mycdp.network.LoadingFinished: "Network.loadingFinished",
    mycdp.fetch.RequestPaused: "Fetch.requestPaused",
}


class CommandError(RuntimeError):
    def __init__(self, method, code):
        self.method, self.code = method, code
        super().__init__(f"CDP {method} failed ({code})")


class DirectChrome:
    def __init__(self, *, host, port):
        with urlopen(f"http://{host}:{port}/json/list", timeout=5) as response:
            pages = [item for item in json.load(response) if item.get("type") == "page"]
        assert len(pages) == 1, "Fresh Chrome must expose exactly one page"
        self.socket = websocket.create_connection(pages[0]["webSocketDebuggerUrl"],
            timeout=35, suppress_origin=True, http_no_proxy=["127.0.0.1", "localhost"])
        self.loop = asyncio.new_event_loop()
        self.handlers = defaultdict(list)
        self.commands = Counter()
        self.ignored_events = Counter()
        self.sequence = 0
        self.page = self

    def get_event_loop(self):
        return self.loop

    def add_handler(self, event_type, callback):
        self.handlers[EVENT_METHODS[event_type]].append(callback)

    def call(self, method, params=None):
        self.sequence += 1
        command_id = self.sequence
        self.commands[method] += 1
        self.socket.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.socket.recv())
            if "id" in message:
                assert message["id"] == command_id, "Unexpected CDP response correlation"
                if "error" in message:
                    raise CommandError(method, message["error"].get("code"))
                return message.get("result", {})
            callbacks = self.handlers.get(message.get("method"))
            if not callbacks:
                self.ignored_events[message.get("method", "unknown")] += 1
                continue
            try:
                event = parse_json_event(message)
            except (KeyError, TypeError, ValueError) as error:
                raise CommandError(message["method"], "event_decode_" + type(error).__name__) from error
            for callback in callbacks:
                result = callback(event)
                if inspect.isawaitable(result):
                    self.loop.create_task(result)

    async def send(self, command):
        request = next(command)
        result = self.call(request["method"], request.get("params"))
        try:
            command.send(result)
        except StopIteration as completion:
            return completion.value
        raise AssertionError("A CDP command must yield exactly one request")

    def evaluate(self, expression, *, await_promise=False):
        # The reference uses page.evaluate as an awaitable only for its async PDF fetch.
        if await_promise:
            return self._evaluate_async(expression)
        return self.loop.run_until_complete(self._evaluate_async(expression))

    async def _evaluate_async(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression,
            "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            raise CommandError("Runtime.evaluate", "javascript_exception")
        return result["result"].get("value")

    def goto(self, url):
        self.loop.run_until_complete(self.send(mycdp.page.enable()))
        navigation = self.call("Page.navigate", {"url": url})
        if navigation.get("errorText"):
            raise CommandError("Page.navigate", "navigation_error")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                state = self.evaluate("({url:location.href,ready:document.readyState})")
                if state["url"] == url and state["ready"] in ("interactive", "complete"):
                    return
            except CommandError as error:
                if error.code != -32000:
                    raise
            self.loop.run_until_complete(asyncio.sleep(0.1))
        raise TimeoutError("Page navigation did not become ready")

    def wait_for_element_visible(self, selector, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate("(() => {const e=document.querySelector(" + json.dumps(selector)
                    + ");return !!(e&&e.offsetParent&&e.getBoundingClientRect().height)})()"):
                return
            self.loop.run_until_complete(asyncio.sleep(0.1))
        raise TimeoutError("Expected form field did not become visible")

    def clear(self, selector):
        self.evaluate("(() => {const e=document.querySelector(" + json.dumps(selector)
                      + ");e.scrollIntoView({block:'nearest'});e.value=''})()")
        self.loop.run_until_complete(asyncio.sleep(0.025))

    def press_keys(self, selector, value):
        self.evaluate("(() => {const e=document.querySelector(" + json.dumps(selector)
                      + ");e.scrollIntoView({block:'nearest'});e.focus()})()")
        for character in value:
            self.call("Input.dispatchKeyEvent", {"type": "char", "text": character})
            self.loop.run_until_complete(asyncio.sleep(0.04 + random.random() / 110))
        self.loop.run_until_complete(asyncio.sleep(0.025))

    def click_if_visible(self, selector):
        assert selector == 'button:contains("Aceitar")', "Only the reference cookie selector is supported"
        point = self.evaluate("""(() => {
            const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Aceitar'&&b.offsetParent);
            if (!b) return null; const r=b.getBoundingClientRect();
            return {x:r.x+r.width/2,y:r.y+r.height/2};})()""")
        if point:
            for event in ("mousePressed", "mouseReleased"):
                self.call("Input.dispatchMouseEvent", {"type": event, "button": "left", "clickCount": 1, **point})

    def save_screenshot(self, path):
        result = self.call("Page.captureScreenshot", {"format": "png"})
        Path(path).write_bytes(base64.b64decode(result["data"]))

    def quit(self):
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.socket.close()
        self.loop.close()
