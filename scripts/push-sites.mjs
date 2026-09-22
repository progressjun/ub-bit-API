// Portable fallback when the installed Sites workflow helper is unavailable.
// Reads a short-lived credential on hidden stdin. Never writes it to disk.
import {spawn} from 'node:child_process';
import {readFileSync} from 'node:fs';
const raw=await new Promise((resolve,reject)=>{
  let value='';
  const terminal=process.stdin.isTTY;
  if(terminal)process.stdin.setRawMode(true);
  process.stderr.write('Ready for Site credential JSON on stdin (input is hidden).\n');
  process.stdin.setEncoding('utf8');
  const onData=chunk=>{value+=chunk;if(value.length>65536)reject(new Error('Input too large'));if(/[\n\r]/.test(value)){process.stdin.off('data',onData);if(terminal)process.stdin.setRawMode(false);process.stdin.pause();resolve(value);}};
  process.stdin.on('data',onData);
});
const credential=JSON.parse(raw);
if(credential.auth_mode!=='http_extra_header'||!credential.token||/[\r\n\0]/.test(credential.token)||Date.parse(credential.token_expires_at)<=Date.now())throw new Error('Invalid or expired source credential');
const remote=new URL(credential.remote_url);
if(remote.protocol!=='https:'||remote.username||remote.password||remote.search||remote.hash)throw new Error('Invalid source remote');
const env={...process.env,GIT_TERMINAL_PROMPT:'0',SITES_GIT_AUTHORIZATION:`Authorization: Bearer ${credential.token}`};
for(const key of Object.keys(env))if(key.startsWith('GIT_TRACE')||key==='GIT_CURL_VERBOSE')delete env[key];
async function git(args,network=false){
  const prefix=network?['-c','credential.helper=','-c','http.extraHeader=','-c','http.followRedirects=false',`--config-env=http.${credential.remote_url}.extraHeader=SITES_GIT_AUTHORIZATION`]:[];
  return new Promise((resolve,reject)=>{let out='',err='';const p=spawn('git',[...prefix,...args],{env,stdio:['ignore','pipe','pipe']});p.stdout.on('data',v=>out+=v);p.stderr.on('data',v=>err+=v);p.on('error',reject);p.on('exit',code=>code===0?resolve(out.trim()):reject(new Error(err.split(credential.token).join('[redacted]'))));});
}
await git(['check-ref-format',`refs/heads/${credential.branch}`]);
if(await git(['status','--porcelain']))throw new Error('Commit validated source before publishing');
if(await git(['ls-remote','--get-url',credential.remote_url])!==credential.remote_url)throw new Error('Git URL rewrite is not allowed');
const manifest=JSON.parse(readFileSync('.openai/hosting.json','utf8'));
const sha=await git(['rev-parse','HEAD']);
await git(['push',credential.remote_url,`${sha}:refs/heads/${credential.branch}`],true);
const advertised=await git(['ls-remote','--heads',credential.remote_url,`refs/heads/${credential.branch}`],true);
if(advertised.split(/\s+/)[0]!==sha)throw new Error('Remote source verification failed');
console.log(JSON.stringify({project_id:manifest.project_id,commit_sha:sha}));
