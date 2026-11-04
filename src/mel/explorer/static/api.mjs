import {signIn} from './auth.mjs';

// JSON calls to the console API on this origin. A 401 starts sign-in.
export async function api(path, options={}) {
  const response=await fetch(`/api${path}`,{...options,headers:{'Content-Type':'application/json',...options.headers}});
  options.signal?.throwIfAborted();
  if (response.status===401) return signIn();
  const data=await response.json().catch(()=>({detail:`Server returned ${response.status}.`}));
  options.signal?.throwIfAborted();
  if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail || data));
  return data;
}

// A screen owns its requests. Renewing or leaving it also invalidates responses
// whose body was already received when navigation happened.
export function requestScope() {
  let controller=new AbortController();
  return {
    renew() { controller.abort(); controller=new AbortController(); },
    cancel() { controller.abort(); },
    get signal() { return controller.signal; },
    api(path, options={}) { return api(path,{...options,signal:options.signal || controller.signal}); },
  };
}
