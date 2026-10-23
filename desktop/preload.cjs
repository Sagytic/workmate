'use strict';
const {contextBridge,ipcRenderer}=require('electron');
contextBridge.exposeInMainWorld('workmateDesktop',Object.freeze({
  status:()=>ipcRenderer.invoke('mini:status'),
  show:()=>ipcRenderer.invoke('mini:show'),
  hide:()=>ipcRenderer.invoke('mini:hide'),
  workspace:(route='home')=>ipcRenderer.invoke('mini:workspace',route),
  settings:patch=>ipcRenderer.invoke('mini:settings',patch),
  menu:()=>ipcRenderer.invoke('mini:menu'),
  nudge:()=>ipcRenderer.invoke('mini:nudge'),
  dismiss:()=>ipcRenderer.invoke('mini:dismiss'),
  onStatus:callback=>{const listener=(_,value)=>callback(value);ipcRenderer.on('mini:updated',listener);return()=>ipcRenderer.removeListener('mini:updated',listener);},
}));
