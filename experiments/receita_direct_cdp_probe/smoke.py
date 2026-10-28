"""Test CDP on a synthetic local page before any Receita interaction."""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import traceback

import mycdp

from experiments.receita_direct_cdp_probe.client import DirectChrome

HTML = b'''<!doctype html><html><body><input name="probe"><button id="send">Send</button>
<script>document.querySelector('#send').onclick=async()=>{
const r=await fetch('/echo',{method:'POST',body:document.querySelector('input').value});
window.smokeResult=await r.json();};</script></body></html>'''


class ControlPage(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML)

    def do_POST(self):
        value = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": value == "probe-123"}).encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-port", type=int, default=9229)
    parser.add_argument("--page-host", default="127.0.0.1")
    parser.add_argument("--require-android", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ControlPage)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {"status": "running", "stage": "connect", "synthetic_page_only": True}
    client = None
    last_method = None
    try:
        client = DirectChrome(host="127.0.0.1", port=args.cdp_port)
        original_recv = client.socket.recv

        def receive():
            nonlocal last_method
            message = original_recv()
            last_method = json.loads(message).get("method", "command_response")
            return message

        client.socket.recv = receive
        browser = client.call("Browser.getVersion")
        report["browser"] = browser["product"]
        report["android"] = "Android" in browser["userAgent"]
        if args.require_android:
            assert report["android"], "Android required for the workflow gate"
        responses, bodies = [], []

        def response(event):
            responses.append((event.request_id, event.response.url, int(event.response.status)))

        async def finished(event):
            if any(item[0] == event.request_id and item[1].endswith("/echo") for item in responses):
                body = await client.send(mycdp.network.get_response_body(event.request_id))
                bodies.append(json.loads(body[0]))

        client.add_handler(mycdp.network.ResponseReceived, response)
        client.add_handler(mycdp.network.LoadingFinished, finished)
        client.loop.run_until_complete(client.send(mycdp.network.enable()))
        report["stage"] = "navigate"
        client.goto(f"http://{args.page_host}:{server.server_port}/")
        report["stage"] = "type_and_tap"
        client.wait_for_element_visible('input[name="probe"]')
        client.clear('input[name="probe"]')
        client.press_keys('input[name="probe"]', "probe-123")
        assert client.evaluate('document.querySelector("input").value') == "probe-123"
        point = client.evaluate("(() => {const r=document.querySelector('#send').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
        if report["android"]:
            client.call("Input.synthesizeTapGesture", {**point, "gestureSourceType": "touch"})
        else:
            for event in ("mousePressed", "mouseReleased"):
                client.call("Input.dispatchMouseEvent", {**point, "type": event, "button": "left", "clickCount": 1})
        report["stage"] = "network_and_async_response"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.evaluate("window.smokeResult||null")
            if result and bodies:
                break
            client.loop.run_until_complete(asyncio.sleep(0.1))
        report["control_page_result"] = result
        report["captured_http_responses"] = len(responses)
        report["captured_response_bodies"] = bodies
        assert result == {"ok": True} and bodies == [{"ok": True}]
        promise = client.loop.run_until_complete(client.page.evaluate("Promise.resolve({ok:true})", await_promise=True))
        assert promise == {"ok": True}
        report.update(status="success", stage="complete", typed_value_matches=True,
                      native_cdp_tap=True, network_http=200, response_body_matches=True,
                      async_evaluation=True, gesture="touch" if report["android"] else "mouse")
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__, last_event=last_method,
                      traceback=traceback.format_exc())
    finally:
        if client is not None:
            report["commands"] = dict(client.commands)
            report["ignored_unsubscribed_events"] = dict(client.ignored_events)
            client.quit()
        server.shutdown()
        server.server_close()
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
    return int(report["status"] != "success")


if __name__ == "__main__":
    raise SystemExit(main())
