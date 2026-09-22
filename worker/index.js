const HTML = '__HTML__';
const CSS = '__CSS__';
const JS = '__JS__';
const cache = new Map();
const pending = new Map();
const json = (data,status=200) => new Response(JSON.stringify(data),{status,headers:{'content-type':'application/json; charset=utf-8','cache-control':'no-store','x-content-type-options':'nosniff'}});
const fail = (message,status=400) => Object.assign(new Error(message),{status});
export async function quotation(path,ttl,fetcher=fetch) {
  const old = cache.get(path);
  if(old && Date.now()-old.at<ttl) return old.data;
  if(pending.has(path)) return pending.get(path);
  const task=(async()=>{
    const response=await fetcher('https://api.upbit.com/v1/'+path,{headers:{Accept:'application/json'},signal:AbortSignal.timeout(8000)});
    if(!response.ok) throw fail(response.status===429?'시세 요청이 많습니다. 잠시 후 다시 확인해 주세요.':'업비트 시세를 불러오지 못했습니다.',502);
    const data=await response.json();
    if(!Array.isArray(data)) throw fail('시세 응답 형식이 올바르지 않습니다.',502);
    if(cache.size>150) cache.delete(cache.keys().next().value);
    cache.set(path,{at:Date.now(),data}); return data;
  })();
  pending.set(path,task);
  try{return await task;}finally{pending.delete(path);}
}
export function relayUrl(value) {
  let url; try{url=new URL(value);}catch{throw fail('엔진 주소 설정을 확인해 주세요.',503);}
  if(url.protocol!=='https:' || url.username || url.password || url.search || url.hash || url.port || !/^[a-z0-9.-]+$/i.test(url.hostname) || !url.hostname.includes('.') || /(^|\.)(localhost|local|internal|test|invalid)$/.test(url.hostname) || /^[\d.]+$/.test(url.hostname)) throw fail('공개 HTTPS 엔진 주소가 필요합니다.',503);
  return url.origin;
}
export function validMarket(market) {return /^KRW-[A-Z0-9]{1,15}$/.test(market);}
export async function route(request,env={},fetcher=fetch) {
  const url=new URL(request.url), path=url.pathname;
  if(request.method==='GET' && ['/','/style.css','/app.js','/favicon.svg'].includes(path)) {
    const assets={'/':[HTML,'text/html; charset=utf-8'],'/style.css':[CSS,'text/css'],'/app.js':[JS,'text/javascript'],'/favicon.svg':['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="10" fill="#b7f36a"/><path d="M11 11v12a9 9 0 0 0 18 0V11h-6v12a3 3 0 0 1-6 0V11z" fill="#111611"/></svg>','image/svg+xml']};
    const [body,type]=assets[path];return new Response(body,{headers:{'content-type':type,'cache-control':'no-cache','x-content-type-options':'nosniff','referrer-policy':'same-origin','content-security-policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'"}});
  }
  if(path==='/api/health' && request.method==='GET') return json({ok:true,service:'ubbit-sites',engineConfigured:!!(env.ENGINE_URL&&env.ENGINE_TOKEN)});
  if(!path.startsWith('/api/')) return json({error:'찾을 수 없는 경로입니다.'},404);
  if(!request.headers.get('oai-authenticated-user-id')) return json({error:'ChatGPT 로그인 후 이용해 주세요.'},401);
  if(!['GET','POST'].includes(request.method)) return json({error:'허용하지 않는 요청입니다.'},405);
  if(request.method==='POST') {
    if(request.headers.get('origin')!==url.origin || !request.headers.get('content-type')?.startsWith('application/json')) return json({error:'올바른 화면에서 다시 요청해 주세요.'},403);
    if(Number(request.headers.get('content-length')||0)>2048) return json({error:'요청이 너무 큽니다.'},413);
  }
  if(path==='/api/markets' && request.method==='GET') {
    const [markets,tickers]=await Promise.all([quotation('market/all?isDetails=true',300000,fetcher),quotation('ticker/all?quote_currencies=KRW',10000,fetcher)]);
    const names=new Map(markets.map(m=>[m.market,m]));
    return json({at:new Date().toISOString(),markets:tickers.map(t=>({...t,...names.get(t.market)})).filter(t=>validMarket(t.market)).sort((a,b)=>b.acc_trade_price_24h-a.acc_trade_price_24h)});
  }
  if(path==='/api/candles' && request.method==='GET') {
    const market=url.searchParams.get('market')||'KRW-BTC',unit=Number(url.searchParams.get('unit')||15);
    if(!validMarket(market)||![1,5,15,60,240].includes(unit)) throw fail('종목 또는 시간 간격을 확인해 주세요.');
    const rows=await quotation(`candles/minutes/${unit}?market=${market}&count=120`,15000,fetcher);
    return json({market,unit,at:new Date().toISOString(),candles:rows.slice().reverse()});
  }
  const routes={'/api/engine':'/v1/snapshot','/api/control':'/v1/control','/api/check':'/v1/check'};
  if(!(path in routes) || (path==='/api/engine')!==(request.method==='GET')) return json({error:'찾을 수 없는 경로입니다.'},404);
  if(!env.ENGINE_URL||!env.ENGINE_TOKEN) return json({connected:false,configured:false,message:'자동매매 엔진을 연결해 주세요. 시세 조회는 바로 사용할 수 있습니다.'});
  const target=relayUrl(env.ENGINE_URL)+routes[path];
  let body;
  if(request.method==='POST') {
    const text=await request.text(); if(text.length>2048) throw fail('요청이 너무 큽니다.',413);
    let parsed;try{parsed=JSON.parse(text);}catch{throw fail('올바른 JSON 요청이 필요합니다.');}
    if(path==='/api/control' && (!['pause','resume'].includes(parsed.action)||typeof parsed.requestId!=='string'||!/^[a-zA-Z0-9-]{16,80}$/.test(parsed.requestId))) throw fail('제어 요청을 확인해 주세요.');
    body=JSON.stringify(path==='/api/check'?{}:{action:parsed.action,requestId:parsed.requestId,confirm:parsed.confirm||''});
  }
  try {
    const response=await fetcher(target,{method:request.method,redirect:'error',headers:{Authorization:`Bearer ${env.ENGINE_TOKEN}`,'content-type':'application/json','x-ubbit-actor':request.headers.get('oai-authenticated-user-id')},body,signal:AbortSignal.timeout(path==='/api/check'?20000:8000)});
    if(!response.ok) return json({connected:false,configured:true,error:response.status===401?'엔진 연결 인증을 확인해 주세요.':response.status===409?'엔진 상태를 확인해 주세요. 연결 점검 후 다시 시도할 수 있습니다.':'엔진이 요청을 처리하지 못했습니다.'},response.status===409?409:502);
    return json(await response.json());
  } catch {return json({connected:false,configured:true,error:'엔진에 연결할 수 없습니다. 마지막 요청의 결과는 확인되지 않았습니다. 새로고침으로 상태를 확인해 주세요.'},502);}
}
export default {async fetch(request,env){try{return await route(request,env);}catch(error){return json({error:error.status?error.message:'연결에 문제가 생겼습니다. 잠시 후 다시 확인해 주세요.'},error.status||502);}}};
