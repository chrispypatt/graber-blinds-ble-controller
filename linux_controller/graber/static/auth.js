'use strict';

const form=document.querySelector('#loginForm');
const error=document.querySelector('#loginError');
const button=document.querySelector('#loginButton');
const accountSetupForm=document.querySelector('#accountSetupForm');
let csrfToken='';

form.addEventListener('submit',async event=>{
  event.preventDefault();
  error.textContent='';
  button.disabled=true;
  button.textContent='Signing in…';
  try{
    const response=await fetch('/api/auth/login',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        username:document.querySelector('#username').value.trim(),
        password:document.querySelector('#password').value,
      }),
    });
    const text=await response.text();
    let body={};
    try{body=text?JSON.parse(text):{}}catch{body={}}
    if(!response.ok)throw new Error(body.error||'Sign in failed.');
    if(body.role==='recovery'&&body.setup_required){
      csrfToken=body.csrf_token;
      form.classList.add('is-hidden');
      accountSetupForm.classList.remove('is-hidden');
      document.querySelector('#accountUsername').focus();
      return;
    }
    window.location.replace('/');
  }catch(reason){
    error.textContent=reason.message;
    button.disabled=false;
    button.textContent='Sign in';
  }
});

accountSetupForm.addEventListener('submit',async event=>{
  event.preventDefault();
  const username=document.querySelector('#accountUsername').value.trim();
  const password=document.querySelector('#accountPassword').value;
  const confirmation=document.querySelector('#accountPasswordConfirm').value;
  const accountError=document.querySelector('#accountSetupError');
  const accountButton=document.querySelector('#accountSetupButton');
  accountError.textContent='';
  if(password!==confirmation){accountError.textContent='Passwords do not match.';return}
  accountButton.disabled=true;
  accountButton.textContent='Creating…';
  try{
    const response=await fetch('/api/auth/account',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},
      body:JSON.stringify({username,password}),
    });
    const text=await response.text();
    let body={};
    try{body=text?JSON.parse(text):{}}catch{body={}}
    if(!response.ok)throw new Error(body.error||'Account creation failed.');
    window.location.replace('/');
  }catch(reason){
    accountError.textContent=reason.message;
    accountButton.disabled=false;
    accountButton.textContent='Create account';
  }
});
