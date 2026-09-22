import http from 'node:http';
import {execFileSync} from 'node:child_process';
execFileSync(process.execPath,['scripts/build.mjs'],{stdio:'inherit'});
const {default:worker}=await import('../dist/server/index.js');
const server=http.createServer(async(req,res)=>{
  try{
    const chunks=[];for await(const c of req)chunks.push(c);
    const headers=new Headers(req.headers);headers.set('oai-authenticated-user-id','local-preview');
    const r=await worker.fetch(new Request(`http://127.0.0.1:8787${req.url}`,{method:req.method,headers,...(req.method!=='GET'&&req.method!=='HEAD'?{body:Buffer.concat(chunks)}:{})}),process.env);
    res.writeHead(r.status,Object.fromEntries(r.headers));res.end(Buffer.from(await r.arrayBuffer()));
  }catch{res.writeHead(500);res.end('Preview unavailable');}
});
server.listen(8787,'127.0.0.1',()=>console.log('Local: http://127.0.0.1:8787'));
