'use strict';
document.getElementById('pet').onclick=()=>window.workmateDesktop.show();document.getElementById('menu').onclick=()=>window.workmateDesktop.menu();
function render(s){document.getElementById('status').textContent=s.error?'연결 확인':s.ready?'모아':'준비 중…';document.getElementById('pet').title=s.shortcut?'빠른 대화 · '+s.shortcut:'빠른 대화';}
window.workmateDesktop.status().then(render);window.workmateDesktop.onStatus(render);
