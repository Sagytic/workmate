"""Self-contained theme for generated apps; shares the workspace preference."""
import re
THEME = r'''<style id="workmate-app-theme">
html{color-scheme:dark}html[data-theme=light]{color-scheme:light}
.grid>.card{min-width:0}.filters{min-width:0}.filters input{min-width:0}.table-wrap{max-width:100%}
html:not([data-theme=light]) body{background:#11151c;color:#e7ebf1}
html:not([data-theme=light]) header{background:#0d1117;border-color:#303947}
html:not([data-theme=light]) header p,html:not([data-theme=light]) #count,html:not([data-theme=light]) label{color:#a5afbe}
html:not([data-theme=light]) .eyebrow,html:not([data-theme=light]) .field-hint,html:not([data-theme=light]) .footer,html:not([data-theme=light]) .rules,html:not([data-theme=light]) .empty{color:#8c99ab}
html:not([data-theme=light]) .card{background:#191f29;border-color:#303947;box-shadow:0 4px 22px #0002}
html:not([data-theme=light]) input,html:not([data-theme=light]) select,html:not([data-theme=light]) textarea{background:#11151c;border-color:#394556;color:#e7ebf1}
html:not([data-theme=light]) input::placeholder,html:not([data-theme=light]) textarea::placeholder{color:#8c99ab}
html:not([data-theme=light]) input:focus,html:not([data-theme=light]) select:focus,html:not([data-theme=light]) textarea:focus{border-color:#8badd4;outline:3px solid #8badd422}
html:not([data-theme=light]) button,html:not([data-theme=light]) a.button{background:#202733;border-color:#394556;color:#e7ebf1}
html:not([data-theme=light]) button:hover,html:not([data-theme=light]) a.button:hover{background:#293343}
html:not([data-theme=light]) button.primary{background:#8badd4;color:#101821;border-color:#8badd4}
html:not([data-theme=light]) button.primary:hover{background:#abc6e5}
html:not([data-theme=light]) button.danger{color:#efa69d}
html:not([data-theme=light]) th{background:#202733;color:#a5afbe;border-color:#303947}
html:not([data-theme=light]) td,html:not([data-theme=light]) .rules,html:not([data-theme=light]) .row-actions button{border-color:#303947}
html:not([data-theme=light]) td:last-child{background:#191f29;box-shadow:-4px 0 8px #0002}
html:not([data-theme=light]) th:last-child{background:#202733}
html:not([data-theme=light]) tbody tr:hover td{background:#202733}
html:not([data-theme=light]) .status{background:#29303c;color:#bec7d3}
html:not([data-theme=light]) .status.active{background:#26374c;color:#a8c8ed}
html:not([data-theme=light]) .status.done,html:not([data-theme=light]) .feedback{background:#20382f;color:#b1d9c5}
html:not([data-theme=light]) .feedback.error{background:#3f292a;color:#f3b5ae}
button:focus-visible,a:focus-visible{outline:2px solid #8badd4;outline-offset:3px}
input[type=checkbox]{accent-color:#8badd4}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
@media print{html{color-scheme:light!important}body,header,.card,td,th,td:last-child,th:last-child{background:white!important;color:#202a38!important;border-color:#dce2e9!important}.toolbar,.form-actions,.row-actions{display:none!important}}
</style><script>
(function(){function apply(){var t='dark';try{if(localStorage.getItem('workmate-theme')==='light')t='light'}catch(e){}document.documentElement.dataset.theme=t}apply();window.addEventListener('storage',function(e){if(e.key==='workmate-theme')apply()})})();
</script>'''


def apply_app_theme(source: str) -> str:
    if 'id="workmate-app-theme"' in source:
        return re.sub(r'<style id="workmate-app-theme">.*?</script>', lambda _: THEME, source, count=1, flags=re.S)
    return source.replace('</head>', THEME + '</head>', 1)
