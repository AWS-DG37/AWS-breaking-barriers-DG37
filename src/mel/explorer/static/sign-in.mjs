const form=document.getElementById('viewer-login');
const error=document.getElementById('login-error');
const submit=form.querySelector('button');
const showError=message=>{error.textContent=message;error.hidden=false;};

fetch('/api/auth/options').then(async response=>{
  if (!response.ok) throw new Error('Sign-in unavailable. Reload to try again.');
  const options=await response.json();
  if (!options.auth) { location.replace('/'); return; }
  form.hidden=!options.viewer;
}).catch(e=>showError(e.message));

form.addEventListener('submit',async event=>{
  event.preventDefault();
  submit.disabled=true; error.hidden=true;
  try {
    const response=await fetch('/api/auth/viewer',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:form.elements.password.value}),
    });
    const data=await response.json();
    if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'Could not sign in. Try again.');
    form.reset();
    location.replace(`/${location.hash || '#runs'}`);
  } catch(e) { showError(e.message); submit.disabled=false; }
});
