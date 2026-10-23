const {test}=require('node:test'),assert=require('node:assert/strict');
const {clamp,safeUrl}=require('./core.cjs');
test('window stays inside shifted small monitor',()=>{assert.deepEqual(clamp({x:5000,y:-100,width:460,height:730},{x:-1280,y:0,width:1280,height:720}),{x:-460,y:0,width:460,height:720});});
test('native links stay on trusted loopback service',()=>{assert.equal(safeUrl('file:///C:/Windows','http://127.0.0.1:8765'),null);assert.equal(safeUrl('javascript:alert(1)','http://127.0.0.1:8765'),null);assert.equal(safeUrl('http://127.0.0.1:8765.evil.com','http://127.0.0.1:8765'),null);assert.equal(safeUrl('/#tickets','http://127.0.0.1:8765'),'http://127.0.0.1:8765/#tickets');});
