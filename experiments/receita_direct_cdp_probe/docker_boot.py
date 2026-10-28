import importlib.util,json,os,subprocess,sys,time,traceback
from pathlib import Path
os.umask(0o077)
out=Path('/work');report={'stage':'preflight','status':'running','docker':Path('/.dockerenv').exists()};emulator=None
try:
 assert report['docker']
 assert importlib.util.find_spec('seleniumbase') is None and importlib.util.find_spec('playwright') is None
 report['python']=sys.version.split()[0]
 version=subprocess.check_output(['emulator','-version'],text=True,stderr=subprocess.STDOUT);report['emulator']=version.splitlines()[0]
 assert '37.2.12.0' in version
 accel=subprocess.run(['emulator','-accel-check'],capture_output=True,text=True,timeout=20);assert accel.returncode==0,accel.stdout
 report['kvm_usable']=True
 subprocess.run(['adb','start-server'],check=True,capture_output=True,timeout=30)
 log=(out/'emulator.log').open('w')
 emulator=subprocess.Popen(['emulator','-avd','test','-port','5554','-wipe-data','-no-window','-gpu','swiftshader','-no-snapshot','-noaudio','-no-boot-anim','-camera-back','none','-camera-front','none'],stdout=log,stderr=subprocess.STDOUT)
 report['stage']='android_boot';start=time.monotonic();last=-1
 while time.monotonic()-start<600:
  assert emulator.poll() is None,'Emulator exited during boot; inspect emulator.log'
  try:
   check=subprocess.run(['adb','-s','emulator-5554','shell','getprop','sys.boot_completed'],capture_output=True,text=True,timeout=12)
   if check.stdout.strip()=='1':break
  except subprocess.TimeoutExpired:pass
  tick=int((time.monotonic()-start)//30)
  if tick!=last:print(json.dumps({'stage':'android_boot','elapsed_s':round(time.monotonic()-start)}),flush=True);last=tick
  time.sleep(3)
 else:raise TimeoutError('Android boot exceeded 600 seconds')
 report['boot_s']=round(time.monotonic()-start,2)
 for key in ('window_animation_scale','transition_animation_scale','animator_duration_scale'):
  subprocess.run(['adb','-s','emulator-5554','shell','settings','put','global',key,'0.0'],check=True,capture_output=True,timeout=20)
 report['stage']='direct_cdp_probe'
 print(json.dumps(report),flush=True)
 result=subprocess.run([sys.executable,*(sys.argv[1:] or ['-m','experiments.receita_direct_cdp_probe.android','--direct-only'])],timeout=420)
 report.update(status='success' if result.returncode==0 else 'error',probe_exit_code=result.returncode,stage='complete')
except Exception as error:
 report.update(status='error',error_type=type(error).__name__,traceback=traceback.format_exc())
finally:
 if emulator is not None and emulator.poll() is None:
  subprocess.run(['adb','-s','emulator-5554','emu','kill'],capture_output=True,timeout=15)
  try:emulator.wait(timeout=20)
  except subprocess.TimeoutExpired:emulator.kill();emulator.wait()
 (out/'container-result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2),flush=True)
sys.exit(int(report['status']!='success'))
