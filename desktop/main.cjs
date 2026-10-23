'use strict';
const {app,BrowserWindow,Tray,Menu,nativeImage,globalShortcut,ipcMain,screen,shell,dialog}=require('electron');
const fs=require('node:fs'),path=require('node:path'),{spawn}=require('node:child_process');
const {clamp,workspaceRoot,safeUrl,preferences}=require('./core.cjs');
if(process.env.WORKMATE_DESKTOP_USER_DATA)app.setPath('userData',path.resolve(process.env.WORKMATE_DESKTOP_USER_DATA));
const single=app.requestSingleInstanceLock();
if(!single){app.quit();}else{
let pet,panel,bubble,tray,ownedServer,quitting=false,ready=false,poll,bubbleTimer,currentNudge=null,shortcut=false,serverError='';
const origin=process.env.WORKMATE_DESKTOP_ORIGIN||'http://127.0.0.1:8765';
if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin))throw new Error('Desktop server must use the loopback interface.');
const root=workspaceRoot(app.isPackaged,process.resourcesPath),prefFile=path.join(app.getPath('userData'),'preferences.json');
const prefs=preferences(prefFile),shortcutName='Control+Alt+Space';
function save(){fs.mkdirSync(path.dirname(prefFile),{recursive:true});fs.writeFileSync(prefFile,JSON.stringify(prefs));}
function startup(){return app.getLoginItemSettings({path:process.execPath,args:app.isPackaged?[]:[__dirname]}).openAtLogin;}
function state(){return {ready,error:serverError,alwaysOnTop:prefs.alwaysOnTop,notifications:prefs.notifications,startup:startup(),shortcut:shortcut?'Ctrl + Alt + Space':null,packaged:app.isPackaged,serverOwned:!!ownedServer,version:app.getVersion()};}
function update(){for(const w of [pet,panel,bubble])if(w&&!w.isDestroyed())w.webContents.send('mini:updated',state());rebuildMenu();}
function positionWindows(){if(!pet)return;const p=pet.getBounds(),area=screen.getDisplayMatching(p).workArea;
 if(panel&&!panel.isDestroyed())panel.setBounds(clamp({...panel.getBounds(),x:p.x-330,y:p.y-610},area));
 if(bubble&&!bubble.isDestroyed())bubble.setBounds(clamp({x:p.x-218,y:p.y-144,width:330,height:140},area));}
function secure(win,kind){
 win.webContents.session.setPermissionRequestHandler((_wc,_permission,callback)=>callback(false));
 win.webContents.session.setPermissionCheckHandler(()=>false);
 win.webContents.setWindowOpenHandler(({url})=>{const safe=safeUrl(url,origin);if(safe)shell.openExternal(safe);return {action:'deny'};});
 win.webContents.on('will-navigate',(event,url)=>{const expected=kind==='panel'?origin+'/?companion=1':require('node:url').pathToFileURL(path.join(__dirname,kind+'.html')).href;if(url!==expected){event.preventDefault();const safe=safeUrl(url,origin);if(safe)shell.openExternal(safe);}});
 win.on('close',event=>{if(!quitting){event.preventDefault();win.hide();}});
}
function makeWindow(options,kind){const w=new BrowserWindow({...options,show:false,alwaysOnTop:prefs.alwaysOnTop,skipTaskbar:true,webPreferences:{preload:path.join(__dirname,'preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true,partition:'persist:workmate-mini'}});secure(w,kind);return w;}
function dismiss(){currentNudge=null;if(bubble)bubble.hide();clearTimeout(bubbleTimer);}
function showPanel(){dismiss();if(!panel)return;pet.showInactive();positionWindows();panel.show();panel.focus();}
function toggle(){if(panel?.isVisible())panel.hide();else showPanel();}
function rebuildMenu(){if(!tray)return;tray.setContextMenu(Menu.buildFromTemplate([
 {label:'모아와 빠른 대화',accelerator:shortcutName,click:showPanel},
 {label:'워크스페이스 열기',click:()=>shell.openExternal(origin+'/#home')},
 {type:'separator'},
 {label:'바탕화면 모아 표시',type:'checkbox',checked:!!pet?.isVisible(),click:item=>{if(item.checked)pet.showInactive();else{pet.hide();dismiss();}rebuildMenu();}},
 {label:'항상 위에 표시',type:'checkbox',checked:prefs.alwaysOnTop,click:item=>applySettings({alwaysOnTop:item.checked})},
 {label:'말풍선 알림',type:'checkbox',checked:prefs.notifications,click:item=>applySettings({notifications:item.checked})},
 {label:'Windows 로그인 시 시작',type:'checkbox',checked:startup(),click:item=>applySettings({startup:item.checked})},
 {label:'화면 오른쪽 아래로 이동',click:()=>{const a=screen.getPrimaryDisplay().workArea;pet.setBounds({x:a.x+a.width-140,y:a.y+a.height-155,width:120,height:140});positionWindows();}},
 {type:'separator'},{label:'Workmate Mini 종료',click:()=>app.quit()}
 ]));}
function applySettings(patch){if(!patch||typeof patch!=='object'||Object.keys(patch).some(k=>!['alwaysOnTop','notifications','startup'].includes(k))||Object.values(patch).some(v=>typeof v!=='boolean'))throw new Error('Invalid setting');
 if('startup'in patch){app.setLoginItemSettings({openAtLogin:patch.startup,path:process.execPath,args:app.isPackaged?[]:[__dirname]});if(startup()!==patch.startup)throw new Error('Windows 시작 설정을 적용하지 못했습니다.');}
 for(const k of ['alwaysOnTop','notifications'])if(k in patch)prefs[k]=patch[k];
 for(const w of [pet,panel,bubble])w?.setAlwaysOnTop(prefs.alwaysOnTop);
 if(!prefs.notifications)dismiss();save();update();return state();}
function trusted(event){const win=[pet,panel,bubble].find(w=>w&&w.webContents.id===event.sender.id);if(!win||event.senderFrame!==event.sender.mainFrame)throw new Error('Invalid sender');const url=event.senderFrame.url;const valid=url===origin+'/?companion=1'||['pet','bubble'].some(k=>url===require('node:url').pathToFileURL(path.join(__dirname,k+'.html')).href);if(!valid)throw new Error('Invalid document');}
function handle(name,fn){ipcMain.handle(name,(event,...args)=>{trusted(event);return fn(...args);});}
handle('mini:status',state);handle('mini:show',()=>{showPanel();return true;});handle('mini:hide',()=>{panel.hide();return true;});handle('mini:settings',applySettings);handle('mini:menu',()=>{tray.popUpContextMenu();return true;});
handle('mini:workspace',route=>{if(typeof route!=='string'||!['home','agents','connections','tickets','memory','conversation','excel','files','documents','analysis','builder','developer','automation','examples'].includes(route))throw new Error('Invalid route');shell.openExternal(origin+'/#'+route);return true;});
handle('mini:nudge',()=>currentNudge);handle('mini:dismiss',()=>{dismiss();return true;});
async function health(){try{const r=await fetch(origin+'/api/health',{signal:AbortSignal.timeout(1800)});const x=await r.json();return x.ok&&(x.service==='workmate'||x.version&&typeof x.llm_configured==='boolean');}catch{return false;}}
async function startServer(){if(await health())return;const localPython=path.join(root,'.venv','Scripts','python.exe');const python=process.env.WORKMATE_PYTHON||(fs.existsSync(localPython)?localPython:'python');
 const port=new URL(origin).port;
 ownedServer=spawn(python,['-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',port],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,PYTHONIOENCODING:'utf-8'}});
 let exited=false;ownedServer.once('error',()=>{exited=true;});ownedServer.once('exit',()=>{exited=true;});
 // Drain process output without persisting request URLs or private contents.
 ownedServer.stdout.on('data',()=>{});ownedServer.stderr.on('data',()=>{});
 for(let i=0;i<50;i++){if(await health())return;if(exited)break;await new Promise(r=>setTimeout(r,400));}
 throw new Error('Workmate 서버를 시작하지 못했습니다. Python과 의존성을 설치한 뒤 다시 실행하세요. 프로젝트 setup.ps1에서 준비할 수 있습니다.');}
async function readNudges(){if(!ready||!prefs.notifications)return;try{const response=await fetch(origin+'/api/companion',{signal:AbortSignal.timeout(3000)});if(!response.ok)return;const d=await response.json();if(d.preferences?.quiet||!d.preferences?.enabled||Date.parse(d.preferences?.quiet_until||'')>Date.now()||d.focus){dismiss();return;}if(panel.isVisible()||!pet.isVisible()||currentNudge)return;const n=(d.nudges||[]).find(x=>!prefs.seen.includes(x.id));if(!n)return;prefs.seen=[...prefs.seen,n.id].slice(-100);save();currentNudge={title:n.title,body:n.body};positionWindows();bubble.webContents.send('mini:updated',state());bubble.showInactive();bubbleTimer=setTimeout(dismiss,14000);}catch{/* Retry on the next poll without taking focus. */}}
app.on('second-instance',()=>{if(pet)showPanel();});app.on('window-all-closed',()=>{});
app.on('before-quit',()=>{quitting=true;clearInterval(poll);clearTimeout(bubbleTimer);globalShortcut.unregisterAll();tray?.destroy();ownedServer?.kill();});
app.whenReady().then(async()=>{
 app.setAppUserModelId('com.workmate.mini');
 const icon=nativeImage.createFromPath(path.join(__dirname,'icon.png'));
 const area=screen.getPrimaryDisplay().workArea,box=clamp({x:area.x+area.width-140,y:area.y+area.height-155,width:120,height:140,...prefs.position},area);
 pet=makeWindow({...box,transparent:true,frame:false,resizable:false,hasShadow:false,title:'Workmate Mini · 모아',icon},'pet');
 panel=makeWindow({width:460,height:730,minWidth:400,minHeight:520,frame:false,resizable:true,backgroundColor:'#11151c',title:'Workmate Mini',icon},'panel');
 bubble=makeWindow({width:330,height:140,transparent:true,frame:false,resizable:false,hasShadow:false,focusable:false,title:'Workmate 알림',icon},'bubble');
 await pet.loadFile(path.join(__dirname,'pet.html'));await bubble.loadFile(path.join(__dirname,'bubble.html'));pet.showInactive();
 pet.on('moved',()=>{prefs.position=pet.getBounds();save();positionWindows();});screen.on('display-metrics-changed',()=>{pet.setBounds(clamp(pet.getBounds(),screen.getDisplayMatching(pet.getBounds()).workArea));positionWindows();});
 tray=new Tray(icon);tray.setToolTip('Workmate Mini · 모아');tray.on('double-click',showPanel);
 shortcut=globalShortcut.register(shortcutName,toggle);update();
 try{await startServer();ready=true;await panel.loadURL(origin+'/?companion=1');update();poll=setInterval(readNudges,15000);setTimeout(readNudges,2500);if(process.argv.includes('--show'))showPanel();}
 catch(error){serverError=error.message;update();await dialog.showMessageBox(pet,{type:'error',title:'Workmate Mini',message:serverError});}
});
}
