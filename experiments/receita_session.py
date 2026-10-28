"""Bounded Receita Docker diagnostic; never logs tokens, cookies or identity data."""
import argparse
import json
import os
import time
import subprocess
import base64
from pathlib import Path

from certidoes.sources import receita as r

parser = argparse.ArgumentParser()
parser.add_argument('--incognito', action='store_true')
parser.add_argument('--locale', action='store_true')
parser.add_argument('--inspect', action='store_true')
parser.add_argument('--form', action='store_true')
parser.add_argument('--graphics', action='store_true')
parser.add_argument('--software-rendering', action='store_true')
parser.add_argument('--minimal-flags', action='store_true')
parser.add_argument('--chrome-sandbox', action='store_true')
parser.add_argument('--profile', type=Path, default=Path('/tmp/receita-profile'))
parser.add_argument('--output', type=Path, default=Path('/app/runs/docker_receita_session/certificate.pdf'))
args = parser.parse_args()
if args.software_rendering or args.minimal_flags or args.chrome_sandbox:
    original_popen = subprocess.Popen
    def launch(command, **kwargs):
        if command[0] == r._chrome_path(None):
            if args.minimal_flags:
                command = [flag for flag in command if not flag.startswith('--disable-')]
            if args.chrome_sandbox:
                command = [flag for flag in command if flag != '--no-sandbox']
            if args.software_rendering:
                command[1:1] = ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']
        return original_popen(command, **kwargs)
    subprocess.Popen = launch
request = r.ReceitaRequest(os.environ['LIVE_DOCUMENT'], os.environ['LIVE_BIRTH'])
document, kind, contributor = r._normalize(request)
chrome = r.CdpChrome(r._chrome_path(None), debug_port=9336, profile_dir=args.profile, incognito=args.incognito)
if args.locale:
    preferences = chrome.profile_dir / 'Default' / 'Preferences'
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_text(json.dumps({'intl':{'accept_languages':'pt-BR,pt','selected_languages':'pt-BR,pt'}}))
started = time.monotonic()
try:
    page = chrome.start()
    if args.graphics:
        print(json.dumps(page.evaluate('''() => {
            const gl=document.createElement('canvas').getContext('webgl');
            const ext=gl && gl.getExtension('WEBGL_debug_renderer_info');
            return {webgl:!!gl, renderer:ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null};
        }''')),flush=True)
    page.goto(r.PAGE_URLS[kind], wait_until='domcontentloaded', timeout=30000)
    page.wait_for_selector(r.WIDGET_SELECTOR, state='attached', timeout=30000)
    print(json.dumps({'widgets':page.locator(r.WIDGET_SELECTOR).count()}), flush=True)
    if args.inspect:
        print(page.locator('input,button').evaluate_all('els=>els.map(e=>({tag:e.tagName,type:e.type,id:e.id,name:e.name,placeholder:e.placeholder,text:e.innerText}))'), flush=True)
    print(json.dumps(page.evaluate('''() => ({ua:navigator.userAgent, language:navigator.language,
        languages:navigator.languages, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
        screen:[screen.width,screen.height], webdriver:navigator.webdriver})''')), flush=True)
    if args.form:
        page.locator('input[name="niContribuinte"]').fill(document)
        year, month, day = request.data_nascimento.split('-')
        page.locator('input[name="dataNascimento"]').fill(f'{day}/{month}/{year}')
        with page.expect_response(lambda x: x.url == r.VERIFICAR_URL, timeout=40000) as result:
            page.get_by_role('button',name='Emitir Certidão',exact=True).click()
        response = result.value
        data = response.json()
        print(json.dumps({'stage':'form_verify','http':response.status,'status':data.get('status'),'validation':data.get('statusValidacao')}),flush=True)
        raise SystemExit(0 if response.status == 200 else 1)
    token = r._token(page, request.token_timeout_ms)
    print(json.dumps({'stage':'token','length':len(token),'seconds':round(time.monotonic()-started,1)}), flush=True)
    payload = r._payload(request, document, kind, contributor)
    def post(url, payload, token=None):
        return page.evaluate('''async ({url,payload,token}) => {
            const headers={'Content-Type':'application/json'};
            if(token) headers['x-captcha-token']=token;
            const response=await fetch(url,{method:'POST',headers,body:JSON.stringify(payload),signal:AbortSignal.timeout(30000)});
            return {status:response.status, data:await response.json()};
        }''', {'url':url,'payload':payload,'token':token})
    verification = post(r.VERIFICAR_URL, payload, token)
    print(json.dumps({'stage':'verify','http':verification['status'], 'status':verification['data'].get('status'),
        'validation':verification['data'].get('statusValidacao')}), flush=True)
    if verification['status'] == 200:
        emission = post(r.EMISSAO_URL, {k:payload[k] for k in ('ni','tipoContribuinte','dataNascimento')})
        print(json.dumps({'stage':'emission','http':emission['status'],'status':emission['data'].get('statusEmissao'),'pdf':bool(emission['data'].get('pdf'))}), flush=True)
        if emission['data'].get('pdf'):
            pdf = base64.b64decode(emission['data']['pdf'], validate=True)
            if not pdf.startswith(b'%PDF-'):
                raise ValueError('Emission did not contain a PDF')
            r._save(pdf,args.output)
        else:
            raise SystemExit(1)
    else:
        raise SystemExit(1)
finally:
    chrome.close()
    print(json.dumps({'elapsed_seconds':round(time.monotonic()-started,1)}), flush=True)
