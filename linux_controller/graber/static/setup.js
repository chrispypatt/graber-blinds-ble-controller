'use strict';
const $=selector=>document.querySelector(selector);
const $$=selector=>[...document.querySelectorAll(selector)];
let authSession={authenticated:false,csrf_token:'',username:'',role:'owner',setup_required:false};
let state={devices:[],adapters:[],available_adapters:[],groups:[],busy:false,allow_jog:false};
let dirty=new Set();
let modalReturnFocus=null;
let scanCancelled=false;
let scanOverlayTimer=null;
let scanMonitorActive=false;
let adapterPollRequest=null;
let addingAdapter=false;
const saveTimers=new Map();
const saveVersions=new Map();
const savePromises=new Map();

function esc(value){const node=document.createElement('span');node.textContent=String(value??'');return node.innerHTML}
function slug(value){return String(value).toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'')||'blind'}
function toast(message,type='success',timeout=4200){const item=document.createElement('div');item.className=`toast ${type}`;item.textContent=message;$('#toasts').append(item);setTimeout(()=>item.remove(),timeout)}
function requireAuth(response){if(response.status===401){window.location.replace('/login');throw new Error('Session expired.')}return response}
async function loadAuthSession(){const response=requireAuth(await fetch('/api/auth/session'));if(!response.ok)throw new Error('Authentication unavailable');authSession=await response.json();if(authSession.setup_required)window.location.replace('/login')}
async function api(path,{method='GET',body}={}){const response=requireAuth(await fetch(path,{method,headers:{...(method!=='GET'?{'X-CSRF-Token':authSession.csrf_token}:{}),...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined}));const text=await response.text();let value={};try{value=text?JSON.parse(text):{}}catch{value={error:text}}if(!response.ok)throw new Error(value.error||text||`Request failed (${response.status})`);return value}
function setConnection(kind,label){const pill=$('#connectionStatus');pill.className=`status-pill ${kind}`;pill.innerHTML=`<i></i>${esc(label)}`}
function adapterLabel(id){if(!id)return 'Automatic';const index=state.adapters.findIndex(item=>item.id===id);const item=state.adapters[index];return item?`Adapter ${String.fromCharCode(65+index)} · USB ${item.usb_path||'external'}${item.powered?'':' (unavailable)'}`:id}
function setAdapterOptions(select,selected){
  const options=[new Option('Automatic',''),...state.adapters.map(adapter=>{
    const option=new Option(adapterLabel(adapter.id),adapter.id);
    option.disabled=!adapter.powered&&adapter.id!==selected;
    return option;
  })];
  if(selected&&!state.adapters.some(adapter=>adapter.id===selected)){
    options.push(new Option(`${selected} (unavailable)`,selected));
  }
  select.replaceChildren(...options);
  select.value=selected||'';
}
function groupName(id){return state.groups.find(item=>item.id===id)?.name||String(id).replaceAll('_',' ').replace(/\b\w/g,letter=>letter.toUpperCase())}
function addGroupChip(container,id,name,card,key){if([...container.children].some(chip=>chip.dataset.groupId===id))return false;const chip=document.createElement('span');chip.className='group-chip';chip.dataset.groupId=id;chip.dataset.groupName=name;chip.append(document.createTextNode(name));const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label',`Remove ${name}`);remove.addEventListener('click',()=>{chip.remove();markDirty(key,card)});chip.append(remove);container.append(chip);return true}
function openModal(selector){const modal=$(selector);modalReturnFocus=document.activeElement;modal.classList.remove('is-hidden');modal.querySelector('.modal-close,button,a')?.focus()}
function closeModal(modal){modal.classList.add('is-hidden');modalReturnFocus?.focus();modalReturnFocus=null}

async function loadState(){try{state=await api('/api/state');render();if(state.scan?.active)resumeScan(state.scan);return true}catch(error){setConnection('error','Unavailable');return false}}

function updateProgress(){const found=state.devices.length;const configured=state.devices.filter(d=>d.configured).length;const included=state.devices.filter(d=>d.enabled).length;$('#foundCount').textContent=found;$('#configuredCount').textContent=configured;$('#adapterCount').textContent=state.adapters.filter(a=>a.powered).length;$('#deviceSummary').textContent=state.loaded_saved_devices?`${state.loaded_saved_devices} shades loaded from saved configuration`:found?`${configured} of ${found} configured`:'Run a scan to begin.';$('#reviewTitle').textContent=included?`${included} shade${included===1?'':'s'} selected`:'No shades selected';$('#reviewCopy').textContent=`${configured} configured`;$('#setupReviewButton').disabled=!included||state.busy||addingAdapter||!state.can_export;$$('#scanButton,#clearScanButton').forEach(button=>button.classList.toggle('is-hidden',found===0))}

function deviceCard(device){const card=document.createElement('article');card.className=`device-card ${device.configured?'configured':''}`;card.dataset.key=device.key;const bottom=device.bottom_position;const shade=Math.max(8,100-(bottom??50));const top=device.dual_rail&&device.top_position!=null?`<div class="position-chip"><small>Top rail</small><strong>${esc(device.top_position)}%</strong></div>`:'';card.innerHTML=`
  <div class="card-head"><div class="device-title"><div class="device-icon" style="--shade-height:${shade}%"><span></span></div><div><h3>${esc(device.name)}</h3><p>${device.rssi_median==null?(device.loaded_from_disk?'Loaded from saved configuration':'Signal unavailable'):`${esc(device.advertised_name)} · ${Math.round(device.rssi_median)} dBm`}</p></div></div><span class="badge ${device.configured?'ready':''}">${device.configured?'Configured':'Needs setup'}</span></div>
  <div class="card-position"><div class="position-chip"><small>${device.dual_rail?'Bottom rail':'Position'}</small><strong>${bottom==null?'Unknown':`${esc(bottom)}%`}</strong></div>${top}</div>
  <form class="device-form"><div class="form-grid">
    <label class="field full"><span>Friendly name</span><input data-field="name" value="${esc(device.name)}" placeholder="e.g. Living Left" required></label>
    <label class="field"><span>Generated ID</span><input data-field="id" value="${esc(device.id)}" readonly></label>
    <div class="field full"><span>Groups</span><div class="group-picker"><div class="group-chips" data-group-chips></div><div class="group-entry"><input id="group-input-${esc(device.key)}" data-group-input role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="group-menu-${esc(device.key)}" placeholder="Add a group" aria-label="Add a group for ${esc(device.name)}"><button class="button secondary compact browse-groups" type="button">Browse</button><button class="button secondary compact add-group" type="button">Add</button></div><div class="group-menu is-hidden" id="group-menu-${esc(device.key)}" data-group-menu role="listbox"></div></div></div>
    <label class="field full"><span>Preferred Bluetooth adapter</span><select data-field="preferred_adapter"></select></label>
    <div class="toggle-row"><div class="toggle-label"><label class="switch"><input data-field="enabled" type="checkbox" aria-label="Set up ${esc(device.name)}" ${device.enabled?'checked':''}><span></span></label><div><strong>Set up</strong></div></div><div class="toggle-label"><label class="switch"><input data-field="dual_rail" type="checkbox" aria-label="${esc(device.name)} is a TDBU shade" ${device.dual_rail?'checked':''}><span></span></label><div><strong>TDBU</strong></div></div></div>
    ${device.has_access_code?'':'<div class="credential-note">Credential required</div>'}
    <div class="card-actions"><span class="dirty-note">${dirty.has(device.key)?'Unsaved changes':device.configured?'Saved':''}</span><div><button class="button secondary identify" type="button" ${!state.allow_jog||device.bottom_raw==null||!device.has_access_code||state.busy||!state.adapters.some(adapter=>adapter.id===device.preferred_adapter&&adapter.powered)?'disabled':''}>Identify with jog</button></div></div>
  </div></form>`;
  const adapterSelect=card.querySelector('[data-field=preferred_adapter]');
  setAdapterOptions(adapterSelect,device.preferred_adapter);
  adapterSelect.addEventListener('blur',()=>{
    if(adapterSelect.dataset.refreshAdapters){
      delete adapterSelect.dataset.refreshAdapters;
      setAdapterOptions(adapterSelect,adapterSelect.value);
    }
  });
  const form=card.querySelector('form');const name=form.querySelector('[data-field=name]');const id=form.querySelector('[data-field=id]');const groupInput=form.querySelector('[data-group-input]');const groupChips=form.querySelector('[data-group-chips]');const groupMenu=form.querySelector('[data-group-menu]');const groupPicker=form.querySelector('.group-picker');(device.groups||[]).forEach(groupId=>addGroupChip(groupChips,groupId,groupName(groupId),card,device.key));const closeGroups=()=>{groupMenu.classList.add('is-hidden');groupInput.setAttribute('aria-expanded','false')};const addGroup=()=>{const friendly=groupInput.value.trim().replace(/\s+/g,' ');if(!friendly||!/[a-z0-9]/i.test(friendly))return false;const groupId=slug(friendly);const existing=state.groups.find(group=>group.id===groupId);const displayName=existing?.name||friendly;if(!existing)state.groups.push({id:groupId,name:displayName});const added=addGroupChip(groupChips,groupId,displayName,card,device.key);groupInput.value='';closeGroups();if(added)markDirty(device.key,card);return added};const renderGroupMenu=()=>{const query=groupInput.value.trim().toLowerCase();const selected=new Set([...groupChips.children].map(chip=>chip.dataset.groupId));const choices=state.groups.filter(group=>!selected.has(group.id)&&(!query||group.name.toLowerCase().includes(query)));groupMenu.innerHTML='';choices.forEach(group=>{const option=document.createElement('button');option.type='button';option.setAttribute('role','option');option.textContent=group.name;option.addEventListener('click',()=>{groupInput.value=group.name;addGroup()});groupMenu.append(option)});groupMenu.classList.toggle('is-hidden',choices.length===0);groupInput.setAttribute('aria-expanded',String(choices.length>0))};form.querySelector('.browse-groups').addEventListener('click',()=>{if(groupMenu.classList.contains('is-hidden')){groupInput.value='';renderGroupMenu();groupInput.focus()}else closeGroups()});form.querySelector('.add-group').addEventListener('click',addGroup);groupInput.addEventListener('input',renderGroupMenu);groupInput.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();addGroup()}else if(event.key==='Escape')closeGroups()});groupPicker.addEventListener('focusout',()=>setTimeout(()=>{if(!groupPicker.contains(document.activeElement)){if(groupInput.value.trim())addGroup();else closeGroups()}},0));name.addEventListener('input',()=>{id.value=slug(name.value);markDirty(device.key,card)});form.querySelectorAll('input:not([readonly]):not([data-group-input]),select').forEach(input=>input.addEventListener('change',()=>markDirty(device.key,card)));form.addEventListener('submit',event=>{event.preventDefault();if(groupInput.value.trim())addGroup();saveDevice(device.key,card)});card.querySelector('.identify').addEventListener('click',()=>identifyDevice(device,card));return card}
function syncDeviceDraft(key,card){const device=state.devices.find(item=>item.key===key);if(!device)return;const values=valuesFrom(card);device.name=values.name||device.name;device.groups=values.groups.map(slug);device.preferred_adapter=values.preferred_adapter;device.enabled=values.enabled;device.dual_rail=values.dual_rail}
function markDirty(key,card){syncDeviceDraft(key,card);dirty.add(key);card.querySelector('.dirty-note').textContent='Unsaved changes';const version=(saveVersions.get(key)||0)+1;saveVersions.set(key,version);const pending=saveTimers.get(key);if(pending)clearTimeout(pending.timer);const timer=setTimeout(()=>{saveTimers.delete(key);saveDevice(key,card,{version})},400);saveTimers.set(key,{timer,card})}
function valuesFrom(card){const get=name=>card.querySelector(`[data-field=${name}]`);return{name:get('name').value.trim(),groups:[...card.querySelectorAll('.group-chip')].map(chip=>chip.dataset.groupName),preferred_adapter:get('preferred_adapter').value,enabled:get('enabled').checked,dual_rail:get('dual_rail').checked}}
async function saveDevice(key,card,{quiet=true,version=saveVersions.get(key)||0}={}){const pending=saveTimers.get(key);if(pending){clearTimeout(pending.timer);saveTimers.delete(key)}const currentCard=()=>document.querySelector(`.device-card[data-key="${key}"]`)||card;let visible=currentCard();if(visible.isConnected)visible.querySelector('.dirty-note').textContent='Saving…';const request=api(`/api/devices/${key}/configure`,{method:'POST',body:valuesFrom(card)});savePromises.set(key,request);try{const result=await request;if(Array.isArray(result.groups))result.groups.forEach(group=>{if(!state.groups.some(item=>item.id===group.id))state.groups.push(group)});if((saveVersions.get(key)||0)===version){const device=state.devices.find(item=>item.key===key);if(device)Object.assign(device,result.device);dirty.delete(key);visible=currentCard();if(visible.isConnected){visible.classList.add('configured');visible.querySelector('.badge').classList.add('ready');visible.querySelector('.badge').textContent='Configured';visible.querySelector('.dirty-note').textContent='Saved'}updateProgress()}if(!quiet)toast('Shade saved.');return true}catch(error){visible=currentCard();if(visible.isConnected)visible.querySelector('.dirty-note').textContent='Save failed';toast(error.message,'error');return false}finally{if(savePromises.get(key)===request)savePromises.delete(key)}}
async function flushSaves(){const scheduled=[...saveTimers.entries()];saveTimers.clear();await Promise.all(scheduled.map(([key,item])=>{clearTimeout(item.timer);return saveDevice(key,item.card,{version:saveVersions.get(key)||0})}));await Promise.all([...savePromises.values()].map(promise=>promise.catch(()=>null)));if(dirty.size)throw new Error('Some shade changes could not be saved.')}
async function identifyDevice(device,card){if(!confirm(`Jog ${device.name} by 5% and return it to its original position? Stay in the room and watch the shade.`))return;try{await flushSaves();state.busy=true;render();setConnection('busy','Jog in progress');const result=await api(`/api/devices/${device.key}/jog`,{method:'POST',body:{}});toast(result.restored?'Shade identified and original position restored.':'Jog completed, but restoration was not confirmed.',result.restored?'success':'error',6500)}catch(error){toast(error.message,'error',6500)}finally{state.busy=false;await loadState({quiet:true})}}

function renderDevices(){const query=$('#searchInput').value.trim().toLowerCase();const filter=$('#filterSelect').value;const visible=state.devices.filter(device=>{const matches=!query||`${device.name} ${device.id} ${device.advertised_name}`.toLowerCase().includes(query);const filtered=filter==='all'||(filter==='needs_setup'&&!device.configured)||(filter==='configured'&&device.configured)||(filter==='included'&&device.enabled);return matches&&filtered});const grid=$('#deviceGrid');grid.innerHTML='';visible.forEach(device=>grid.append(deviceCard(device)));$('#emptyState').classList.toggle('is-hidden',state.devices.length>0);grid.classList.toggle('is-hidden',state.devices.length===0);if(state.devices.length&&!visible.length)grid.innerHTML='<div class="empty-state"><h3>No matching shades</h3><p>Try a different search or filter.</p></div>'}
function hasPoweredAdapter(){return state.adapters.some(adapter=>adapter.powered)}
function adapterBusy(){return state.busy||scanOverlayTimer!==null||addingAdapter}

function renderAdapterList(){
  const list=$('#setupAdapterList');
  const available=state.available_adapters||[];
  const signature=JSON.stringify([state.adapters,available]);
  if(list.dataset.inventory!==signature){
    const focusedAdapter=list.contains(document.activeElement)?document.activeElement.dataset.addAdapter:null;
    const rows=[...state.adapters.map(adapter=>[adapter,false]),...available.map(adapter=>[adapter,true])];
    list.replaceChildren(...rows.map(([adapter,canAdd])=>{
      const row=document.createElement('li');
      row.className='adapter-card setup-adapter-card';
      const details=document.createElement('div');
      const name=document.createElement('strong');
      name.textContent=adapter.product||'USB Bluetooth adapter';
      const location=document.createElement('p');
      location.textContent=canAdd?`USB ${adapter.usb_path||'external'} · Ready to add`:adapterLabel(adapter.id);
      details.append(name,location);
      row.append(details);
      if(canAdd){
        const button=document.createElement('button');
        button.type='button';
        button.className='button secondary compact';
        button.dataset.addAdapter=adapter.id;
        button.textContent='Add adapter';
        button.setAttribute('aria-label',`Add adapter at USB ${adapter.usb_path||'external'}`);
        button.addEventListener('click',()=>addAdapter(adapter.id));
        row.append(button);
      }
      return row;
    }));
    list.dataset.inventory=signature;
    const empty=$('#setupAdapterEmpty');
    empty.classList.toggle('is-hidden',rows.length>0);
    empty.textContent='No powered USB Bluetooth adapters detected.';
    if(focusedAdapter){
      const replacement=[...list.querySelectorAll('[data-add-adapter]')]
        .find(button=>button.dataset.addAdapter===focusedAdapter);
      (replacement||$('#setupAdaptersTitle')).focus();
    }
  }
  list.querySelectorAll('[data-add-adapter]').forEach(button=>button.disabled=adapterBusy());
}

function applyAdapterInventory(inventory){
  const adaptersChanged=JSON.stringify(state.adapters)!==JSON.stringify(inventory.adapters);
  state.adapters=inventory.adapters;
  state.available_adapters=inventory.available_adapters;
  state.missing_adapters=inventory.missing_adapters;
  state.can_export=inventory.can_export;
  renderAdapterStatus();
  if(!state.busy&&scanOverlayTimer===null){
    setConnection(hasPoweredAdapter()?'online':'error',hasPoweredAdapter()?'Ready':'Bluetooth unavailable');
  }
  updateProgress();
  if(adaptersChanged){
    $$('#deviceGrid .device-card').forEach(card=>{
      const select=card.querySelector('[data-field=preferred_adapter]');
      if(document.activeElement===select)select.dataset.refreshAdapters='true';
      else setAdapterOptions(select,select.value);
      const device=state.devices.find(item=>item.key===card.dataset.key);
      card.querySelector('.identify').disabled=!state.allow_jog||device.bottom_raw==null||
        !device.has_access_code||state.busy||!state.adapters.some(adapter=>adapter.id===select.value&&adapter.powered);
    });
  }
}

function pollAdapters(){
  if(document.hidden||adapterBusy()||adapterPollRequest)return;
  adapterPollRequest=api('/api/adapters').then(inventory=>{
    if(!adapterBusy())applyAdapterInventory(inventory);
  }).catch(()=>{
    // Keep the last working inventory through a transient discovery failure.
  }).finally(()=>adapterPollRequest=null);
}

async function addAdapter(id){
  if(adapterBusy())return;
  const hadFocus=document.activeElement.dataset.addAdapter===id;
  addingAdapter=true;
  renderAdapterStatus();
  updateProgress();
  try{
    await adapterPollRequest;
    const inventory=await api(`/api/adapters/${encodeURIComponent(id)}`,{method:'POST'});
    applyAdapterInventory(inventory);
    if(hadFocus&&document.activeElement===document.body)$('#setupAdaptersTitle').focus();
    toast('Adapter added. Save setup to keep it.');
  }catch(error){
    toast(error.message,'error');
  }finally{
    addingAdapter=false;
    renderAdapterStatus();
    updateProgress();
    if(hadFocus&&document.activeElement===document.body){
      const retry=$$('#setupAdapterList [data-add-adapter]').find(button=>button.dataset.addAdapter===id);
      retry?.focus();
    }
  }
}

function renderAdapterStatus(){
  const missing=state.missing_adapters||0;
  const noAdapters=!hasPoweredAdapter();
  $('#adapterNotice').classList.toggle('is-hidden',!missing&&!noAdapters);
  $('#adapterNoticeTitle').textContent=noAdapters&&!missing?'Add a powered USB Bluetooth adapter':
    missing===1?'One configured Bluetooth adapter is unavailable':`${missing} configured Bluetooth adapters are unavailable`;
  $('#adapterNoticeDetail').textContent=missing?'Reconnect or power on the missing adapters to save setup.':
    (state.available_adapters||[]).length?'Add an adapter below to scan for shades.':
      'Connect or power on a USB Bluetooth adapter to scan for shades.';
  $$('#scanButton,#emptyScanButton').forEach(button=>button.disabled=noAdapters||adapterBusy());
  const exit=$('#exitSetupButton');
  exit.classList.toggle('is-hidden',!state.can_exit_setup);
  exit.disabled=adapterBusy();
  renderAdapterList();
}
async function exitSetup(){
  if(state.devices.length&&!confirm('Exit setup without saving the shade configuration?'))return;
  const button=$('#exitSetupButton');
  button.disabled=true;
  try{
    await api('/api/portal/control',{method:'POST'});
    window.location.replace('/');
  }catch(error){
    toast(error.message,'error');
    button.disabled=false;
  }
}
function render(){renderAdapterStatus();setConnection(state.busy?'busy':'online',state.busy?'Bluetooth busy':hasPoweredAdapter()?'Ready':'Bluetooth unavailable');updateProgress();renderDevices()}
function startScanOverlay(duration,remaining=duration){clearInterval(scanOverlayTimer);scanCancelled=false;const progress=$('#scanProgress');const elapsedOffset=Math.max(0,duration-remaining)*1000;const started=performance.now()-elapsedOffset;let lastStatus='';$('#scanOverlayTitle').textContent='Scanning for shades';$('#cancelScanButton').disabled=false;$('#cancelScanButton').textContent='Cancel scan';$('#scanOverlay').classList.remove('is-hidden');const update=()=>{const elapsed=performance.now()-started;const secondsLeft=Math.max(0,Math.ceil((duration*1000-elapsed)/1000));const percent=Math.min(100,(elapsed/(duration*1000))*100);const label=secondsLeft?`${secondsLeft}s`:'Finishing…';$('#scanCountdown').textContent=label;$('#scanProgressFill').style.width=`${percent}%`;progress.setAttribute('aria-valuenow',String(Math.round(percent)));if(label!==lastStatus){setConnection('busy',secondsLeft?`Scanning · ${label}`:'Finishing scan');lastStatus=label}};update();scanOverlayTimer=setInterval(update,250);renderAdapterStatus()}
function stopScanOverlay(){clearInterval(scanOverlayTimer);scanOverlayTimer=null;$('#scanOverlay').classList.add('is-hidden');renderAdapterStatus();$('#scanProgressFill').style.width='0%';$('#scanProgress').setAttribute('aria-valuenow','0')}
async function scan(){const seconds=Math.max(3,Math.min(60,Number($('#scanSeconds').value)||12));await flushSaves();startScanOverlay(seconds);try{const result=await api('/api/scan',{method:'POST',body:{seconds}});state=result;render();if(!(scanCancelled||result.cancelled))toast(`Found ${state.devices.length} nearby shade${state.devices.length===1?'':'s'}.`)}catch(error){if(!scanCancelled){toast(error.message,'error');setConnection('error','Scan failed')}}finally{stopScanOverlay()}}
async function resumeScan(scanInfo){if(scanMonitorActive)return;scanMonitorActive=true;startScanOverlay(Math.max(1,Number(scanInfo.duration_s)||1),Math.max(0,Number(scanInfo.remaining_s)||0));try{while(true){await new Promise(resolve=>setTimeout(resolve,750));const current=await api('/api/state');if(!current.scan?.active){state=current;render();break}}}catch(error){if(!scanCancelled)toast(error.message,'error')}finally{scanMonitorActive=false;stopScanOverlay()}}
async function cancelScan(){if($('#scanOverlay').classList.contains('is-hidden'))return;scanCancelled=true;const button=$('#cancelScanButton');button.disabled=true;button.textContent='Stopping…';$('#scanOverlayTitle').textContent='Stopping scan';try{await api('/api/scan/cancel',{method:'POST',body:{}})}catch(error){scanCancelled=false;button.disabled=false;button.textContent='Cancel scan';$('#scanOverlayTitle').textContent='Scanning for shades';toast(error.message,'error')}}
async function clearScan(){if(!confirm('Clear scan results and reload the saved configuration?'))return;const button=$('#clearScanButton');button.disabled=true;try{await flushSaves();state=await api('/api/scan/clear',{method:'POST',body:{}});dirty.clear();render();toast(state.loaded_saved_devices?'Saved configuration reloaded.':'Scan cleared.')}catch(error){toast(error.message,'error')}finally{button.disabled=false}}
async function reviewSetup(){await flushSaves();const selected=state.devices.filter(d=>d.enabled);$('#setupReview').innerHTML=`<table class="review-table"><thead><tr><th scope="col">Shade</th><th scope="col">Groups</th><th scope="col">Type</th><th scope="col">Adapter</th></tr></thead><tbody>${selected.map(d=>{const groups=(d.groups||[]).map(groupName);return `<tr><th scope="row"><strong>${esc(d.name)}</strong><small>${esc(d.id)}</small></th><td>${esc(groups.length?groups.join(', '):'Individual only')}</td><td>${d.dual_rail?'TDBU':'Single rail'}</td><td>${esc(adapterLabel(d.preferred_adapter))}</td></tr>`}).join('')}</tbody></table>`;
  openModal('#setupReviewModal')
}
async function saveSetup(){const button=$('#confirmSetup');button.disabled=true;button.textContent='Saving…';try{await flushSaves();const result=await api('/api/export',{method:'POST',body:{}});if(result.mode==='control'){button.textContent='Opening controls…';window.location.replace('/');return}closeModal($('#setupReviewModal'));toast('Setup saved.','success')}catch(error){toast(error.message,'error',6500)}finally{button.disabled=false;button.textContent='Save setup'}}

function openAccount(){modalReturnFocus=document.activeElement;const recovery=authSession.role==='recovery';$('#accountName').textContent=recovery?`${authSession.username} · Recovery`:authSession.username;$('#passwordError').textContent='';$('#ownerAccountError').textContent='';$('#passwordForm').reset();$('#ownerAccountForm').reset();$('#passwordForm').classList.toggle('is-hidden',recovery);$('#ownerAccountForm').classList.toggle('is-hidden',!recovery);$('#accountModal').classList.remove('is-hidden');$(recovery?'#ownerUsername':'#currentPassword').focus()}
async function changePassword(event){event.preventDefault();const current=$('#currentPassword').value,newPassword=$('#newPassword').value,confirmation=$('#confirmPassword').value,error=$('#passwordError');error.textContent='';if(newPassword!==confirmation){error.textContent='Passwords do not match.';return}const button=event.submitter;button.disabled=true;try{const result=await api('/api/auth/password',{method:'POST',body:{current_password:current,new_password:newPassword}});if(result.csrf_token)authSession=result;closeModal($('#accountModal'));toast('Password changed.')}catch(reason){error.textContent=reason.message}finally{button.disabled=false}}
async function saveOwner(event){event.preventDefault();const username=$('#ownerUsername').value.trim(),password=$('#ownerPassword').value,confirmation=$('#ownerPasswordConfirm').value,error=$('#ownerAccountError');error.textContent='';if(password!==confirmation){error.textContent='Passwords do not match.';return}const button=event.submitter;button.disabled=true;try{const result=await api('/api/auth/account',{method:'POST',body:{username,password}});if(result.csrf_token)authSession=result;closeModal($('#accountModal'));toast('Owner saved.')}catch(reason){error.textContent=reason.message}finally{button.disabled=false}}
async function logout(){const button=$('#logoutButton');button.disabled=true;try{await flushSaves()}catch(reason){toast(reason.message,'error')}try{await api('/api/auth/logout',{method:'POST',body:{}})}finally{window.location.replace('/login')}}

$('#exitSetupButton').addEventListener('click',exitSetup);$('#setupHelpButton').addEventListener('click',()=>openModal('#setupHelpModal'));$('#accountButton').addEventListener('click',openAccount);$('#passwordForm').addEventListener('submit',changePassword);$('#ownerAccountForm').addEventListener('submit',saveOwner);$('#logoutButton').addEventListener('click',logout);$('#scanButton').addEventListener('click',scan);$('#emptyScanButton').addEventListener('click',scan);$('#cancelScanButton').addEventListener('click',cancelScan);$('#clearScanButton').addEventListener('click',clearScan);$('#searchInput').addEventListener('input',renderDevices);$('#filterSelect').addEventListener('change',renderDevices);$('#setupReviewButton').addEventListener('click',reviewSetup);$('#confirmSetup').addEventListener('click',saveSetup);$$('[data-close-modal]').forEach(button=>button.addEventListener('click',()=>closeModal(button.closest('.modal-backdrop'))));$$('.modal-backdrop').forEach(modal=>modal.addEventListener('click',event=>{if(event.target===modal)closeModal(modal)}));document.addEventListener('keydown',event=>{if(event.key==='Escape'){const modal=$$('.modal-backdrop:not(.is-hidden)').at(-1);if(modal)closeModal(modal)}});
loadAuthSession().then(loadState).then(loaded=>{
  if(!loaded)return;
  setInterval(pollAdapters,5000);
  document.addEventListener('visibilitychange',pollAdapters);
}).catch(()=>setConnection('error','Unavailable'));
