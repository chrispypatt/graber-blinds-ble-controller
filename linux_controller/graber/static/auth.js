'use strict';

const form=document.querySelector('#loginForm');
const error=document.querySelector('#loginError');
const button=document.querySelector('#loginButton');
const initialForm=document.querySelector('#initialPasswordForm');
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
    if(body.must_change_password){
      csrfToken=body.csrf_token;
      form.classList.add('is-hidden');
      initialForm.classList.remove('is-hidden');
      document.querySelector('#initialPassword').focus();
      return;
    }
    window.location.replace('/');
  }catch(reason){
    error.textContent=reason.message;
    button.disabled=false;
    button.textContent='Sign in';
  }
});

initialForm.addEventListener('submit',async event=>{
  event.preventDefault();
  const password=document.querySelector('#initialPassword').value;
  const confirmation=document.querySelector('#initialPasswordConfirm').value;
  const initialError=document.querySelector('#initialPasswordError');
  const initialButton=document.querySelector('#initialPasswordButton');
  initialError.textContent='';
  if(password!==confirmation){initialError.textContent='Passwords do not match.';return}
  initialButton.disabled=true;
  initialButton.textContent='Saving…';
  try{
    const response=await fetch('/api/auth/password',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},
      body:JSON.stringify({new_password:password}),
    });
    const text=await response.text();
    let body={};
    try{body=text?JSON.parse(text):{}}catch{body={}}
    if(!response.ok)throw new Error(body.error||'Password change failed.');
    window.location.replace('/');
  }catch(reason){
    initialError.textContent=reason.message;
    initialButton.disabled=false;
    initialButton.textContent='Save password';
  }
});
