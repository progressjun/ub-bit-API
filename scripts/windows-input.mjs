// Pipe a secret JSON object into the Windows DPAPI installer without terminal echo.
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
if(process.platform!=='win32') throw new Error('Windows only');
const input=await new Promise((resolve,reject)=>{
  let value='';const raw=process.stdin.isTTY;
  const finish=()=>{process.stdin.off('data',read);if(raw)process.stdin.setRawMode(false);process.stdin.pause();resolve(value);};
  const read=chunk=>{value+=chunk;if(value.includes('\u0003')){reject(new Error('Cancelled'));process.exit(130);}if(value.length>4096)process.exit(1);if(/[\r\n]/.test(value))finish();};
  if(raw)process.stdin.setRawMode(true);
  process.stdin.setEncoding('utf8');process.stdin.on('data',read);process.stdin.resume();
  console.log('Ready for credential JSON on stdin (input is hidden).');
});
JSON.parse(input);
const env={...process.env};
for(const key of Object.keys(env))if(key.toLowerCase()==='psmodulepath')delete env[key];
const child=spawn('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',fileURLToPath(new URL('./windows-engine.ps1',import.meta.url)),'-Action','Install'],{env,stdio:['pipe','inherit','inherit'],windowsHide:true});
child.stdin.end(input.trim()+'\n');
child.on('exit',code=>process.exit(code??1));
