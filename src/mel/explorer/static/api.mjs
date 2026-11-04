import {signIn} from './auth.mjs';

// JSON calls to the console API on this origin. A 401 starts sign-in.
export async function api(path, options={}) {
  const response=await fetch(`/api${path}`,{...options,headers:{'Content-Type':'application/json',...options.headers}});
  if (response.status===401) return signIn();
  const data=await response.json().catch(()=>({detail:`Server returned ${response.status}.`}));
  if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail || data));
  return data;
}
