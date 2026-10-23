'use strict';
const fs=require('node:fs'),path=require('node:path');
function clamp(bounds,area){const width=Math.min(bounds.width,area.width),height=Math.min(bounds.height,area.height);return {x:Math.max(area.x,Math.min(bounds.x,area.x+area.width-width)),y:Math.max(area.y,Math.min(bounds.y,area.y+area.height-height)),width,height};}
function workspaceRoot(packaged,resources){if(process.env.WORKMATE_ROOT)return path.resolve(process.env.WORKMATE_ROOT);if(!packaged)return path.resolve(__dirname,'..');const cfg=path.join(resources,'workmate-root.json');if(fs.existsSync(cfg))return path.resolve(resources,JSON.parse(fs.readFileSync(cfg,'utf8')).path);return path.resolve(resources,'../../../workmate');}
function safeUrl(value,origin){try{const u=new URL(value,origin);return u.origin===origin&&u.username===''&&u.password===''?u.href:null;}catch{return null;}}
function preferences(file){try{const x=JSON.parse(fs.readFileSync(file,'utf8'));return {alwaysOnTop:x.alwaysOnTop!==false,notifications:x.notifications!==false,position:x.position,seen:Array.isArray(x.seen)?x.seen.filter(s=>typeof s==='string').slice(-100):[]};}catch{return {alwaysOnTop:true,notifications:true,seen:[]};}}
module.exports={clamp,workspaceRoot,safeUrl,preferences};
