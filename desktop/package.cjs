'use strict';
const {packager}=require('@electron/packager');
const path=require('node:path'),fs=require('node:fs'),os=require('node:os'),crypto=require('node:crypto');

async function sha256(file){
  const hash=crypto.createHash('sha256');
  for await(const chunk of fs.createReadStream(file))hash.update(chunk);
  return hash.digest('hex');
}

async function verifiedElectronZipDir(){
  // Reuse the standard official Electron cache only after checking the digest
  // published in the installed Electron npm package. Never relax TLS checks.
  const electronDir=path.dirname(require.resolve('electron/package.json'));
  const version=require(path.join(electronDir,'package.json')).version;
  const filename=`electron-v${version}-win32-x64.zip`;
  const expected=require(path.join(electronDir,'checksums.json'))[filename];
  if(!expected)throw new Error(`No official checksum is available for ${filename}`);
  const explicit=process.env.WORKMATE_ELECTRON_ZIP_DIR;
  const standard=process.platform==='win32'
    ?path.join(process.env.LOCALAPPDATA||path.join(os.homedir(),'AppData','Local'),'electron','Cache')
    :process.platform==='darwin'?path.join(os.homedir(),'Library','Caches','electron')
    :path.join(process.env.XDG_CACHE_HOME||path.join(os.homedir(),'.cache'),'electron');
  const candidates=[];
  if(explicit)candidates.push(path.resolve(explicit));
  else if(fs.existsSync(standard)){
    candidates.push(standard);
    for(const entry of fs.readdirSync(standard,{withFileTypes:true})){
      if(entry.isDirectory()&&!entry.isSymbolicLink())candidates.push(path.join(standard,entry.name));
    }
  }
  for(const directory of candidates){
    const file=path.join(directory,filename);
    if(!fs.existsSync(file))continue;
    if(await sha256(file)!==expected)throw new Error(`Cached Electron checksum mismatch: ${filename}`);
    console.log(`Using verified cached ${filename} (SHA-256 ${expected})`);
    return directory;
  }
  if(explicit)throw new Error(`The selected cache does not contain ${filename}`);
  // Packager's normal HTTPS download and checksum verification remain enabled.
  return undefined;
}

function repairAsarIntegrity(folder){
  // Packager 19.0.1 assigns a pooled Buffer's entire backing ArrayBuffer to
  // ELECTRONASAR. Rebuild that resource with exactly the intended JSON bytes;
  // otherwise unrelated temporary paths/source bytes can enter the executable.
  const {NtExecutable,NtExecutableResource}=require('resedit');
  const {getRawHeader}=require('@electron/asar');
  const archive=path.join(folder,'resources','app.asar');
  const executable=path.join(folder,'Workmate Mini.exe');
  const hash=crypto.createHash('sha256').update(getRawHeader(archive).headerString).digest('hex');
  const payload=JSON.stringify([{file:'resources\\app.asar',alg:'SHA256',value:hash}]);
  const exe=NtExecutable.from(fs.readFileSync(executable));
  const resources=NtExecutableResource.from(exe);
  const entries=resources.entries.filter(entry=>entry.type==='INTEGRITY'&&entry.id==='ELECTRONASAR');
  if(entries.length!==1)throw new Error('Expected exactly one Electron ASAR integrity resource.');
  const bytes=Buffer.from(payload,'utf8');
  entries[0].bin=bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength);
  resources.outputResource(exe);
  const generated=Buffer.from(exe.generate());
  // This distribution must not contain the current builder's personal path.
  for(const marker of [Buffer.from(os.homedir()),Buffer.from(os.homedir(),'utf16le')]){
    if(generated.includes(marker))throw new Error('A personal build path remains in the executable.');
  }
  fs.writeFileSync(executable,generated);
  const verified=NtExecutableResource.from(NtExecutable.from(generated)).entries.find(entry=>entry.type==='INTEGRITY'&&entry.id==='ELECTRONASAR');
  if(Buffer.from(verified.bin).toString('utf8')!==payload)throw new Error('Electron ASAR integrity resource did not round-trip exactly.');
  console.log(`Verified exact Electron ASAR integrity resource (${bytes.length} bytes).`);
}

async function main(){
  const electronZipDir=await verifiedElectronZipDir();
  const [folder]=await packager({dir:__dirname,out:path.resolve(__dirname,'../../workmate-desktop'),
    name:'Workmate Mini',platform:'win32',arch:'x64',overwrite:true,asar:true,prune:true,
    ...(electronZipDir?{electronZipDir}:{}),
    ignore:[/^\/node_modules/,/^\/test/,/^\/package\.cjs$/],appBundleId:'com.workmate.mini',
    win32metadata:{CompanyName:'Workmate',FileDescription:'Workmate Mini',ProductName:'Workmate Mini'}});
  repairAsarIntegrity(folder);
  fs.writeFileSync(path.join(folder,'resources','workmate-root.json'),JSON.stringify({path:'../../../workmate'}));
  console.log(folder);
}

if(require.main===module)main().catch(error=>{console.error(error.message);process.exitCode=1;});
module.exports={sha256,verifiedElectronZipDir,repairAsarIntegrity};
