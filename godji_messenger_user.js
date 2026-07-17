// ==UserScript==
// @name         Годжи — Сообщения
// @namespace    http://tampermonkey.net/
// @version      1.1
// @match        https://godji.cloud/*
// @match        https://*.godji.cloud/*
// @exclude      https://godji.cloud/tv/*
// @exclude      https://*.godji.cloud/tv/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==
(function(){
'use strict';

var PROXY = 'http://localhost:6070';
var POLL_STATUS_MS = 3000;
var POLL_CHAT_MS = 1000;
var POLL_EVENTS_MS = 1500;
var TOAST_MS = 15000;
var MAX_TOASTS_VISIBLE = 4;
var TOAST_CARD_H = 92; // высота карточки + отступ, для расчёта max-height стека

function pingErpAlive(){
    fetch(PROXY+'/erp_heartbeat', {method:'POST'}).catch(function(){});
}
pingErpAlive();
setInterval(pingErpAlive, 8000);

// ── Автоопределение ника клиента по ПК (для отображения в админ-программе) ──
// ВНИМАНИЕ: использует те же глобальные window._godjiAuth / clubId, что и
// остальные скрипты набора (см. MODULE_WRITING_GUIDE.md). Если в твоей
// сборке auth-токен называется иначе — поправь window._godjiAuth ниже.
function getCachedClubId(){
    try{
        var v = localStorage.getItem('_godji_club_id');
        return v ? parseInt(v,10) : 14;
    }catch(e){ return 14; }
}

function fetchNicknames(){
    if(!window._godjiAuth){
        console.log('[godji-messenger] window._godjiAuth не найден — скрипт не может запросить никнеймы. '
            + 'Нужен другой скрипт (перехватчик авторизации), который его выставляет.');
        return;
    }
    fetch('https://hasura.godji.cloud/v1/graphql', {
        method: 'POST',
        headers: {'content-type':'application/json', 'authorization':window._godjiAuth, 'x-hasura-role':'club_admin'},
        body: JSON.stringify({
            query: 'query($clubId:Int!){getDashboardDevices(clubId:$clubId){devices{name sessions{status user{login}}}}}',
            variables: {clubId: getCachedClubId()}
        })
    }).then(function(r){ return r.json(); }).then(function(d){
        console.log('[godji-messenger] ответ getDashboardDevices:', d);
        var devices = (d && d.data && d.data.getDashboardDevices && d.data.getDashboardDevices.devices) || [];
        if(!devices.length){
            console.log('[godji-messenger] devices пустой или запрос вернул ошибку (см. d.errors выше)');
        }
        devices.forEach(function(dev){
            var sessions = dev.sessions || [];
            var active = sessions.find(function(s){ return s.status && s.status.indexOf('session')===0; });
            var nickname = active && active.user && active.user.login;
            if(nickname){
                console.log('[godji-messenger] ПК '+dev.name+' -> ник '+nickname);
                fetch(PROXY+'/nickname',{
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({pc: dev.name, nickname: nickname, manual:false})
                }).catch(function(){});
            }
        });
    }).catch(function(e){ console.log('[godji-messenger] ошибка запроса никнеймов:', e); });
}
setTimeout(fetchNicknames, 3000);
setInterval(fetchNicknames, 20000);

// ── Непрочитанные / статусы прочтения ─────────────────────
var READ_KEY = 'godji_msg_read';
var _readState = loadReadState();   // {pc: lastReadId}
var _unread = {};                   // {pc: count}
var _lastEventId = 0;
var _notifyInErp = true; // false, если админ выбрал "уведомления в программе"

function refreshNotifySetting(){
    fetch(PROXY+'/settings')
        .then(function(r){ return r.json(); })
        .then(function(d){ _notifyInErp = (d.notifyTarget !== 'admin_app'); })
        .catch(function(){ _notifyInErp = true; });
}
var _eventsTimer = null;
var _chatOwnBubbles = [];           // [{id, metaEl}] — для галочек прочтения в открытом чате

function loadReadState(){
    try{ return JSON.parse(localStorage.getItem(READ_KEY) || '{}'); }catch(e){ return {}; }
}
function saveReadState(){
    try{ localStorage.setItem(READ_KEY, JSON.stringify(_readState)); }catch(e){}
}

// Точные координаты ПК — та же карта, что и в просмотре экрана
var MAP_ORIG_W=1920,MAP_ORIG_H=1133;
var MAP_IMG='https://goodgame-prod.storage.yandexcloud.net/tmp-2-1773905668693';
var LAYER_W=1818,LAYER_H=1073;
var CARD_ORIG=57;
var CROP_X=579,CROP_Y=25,CROP_W=700,CROP_H=1016;
var POPUP_W=500;
var MAP_SCALE=POPUP_W/CROP_W;
var POPUP_H=Math.round(CROP_H*MAP_SCALE);

var PC_POS = {
    '01':{x:661,y:367},'02':{x:599,y:369},'03':{x:599,y:260},
    '04':{x:668,y:259},'05':{x:739,y:259},'06':{x:824,y:450},
    '07':{x:896,y:452},'08':{x:1082,y:328},'09':{x:1146,y:328},
    '10':{x:960,y:45},'11':{x:1015,y:45},'12':{x:1071,y:45},'13':{x:1126,y:45},
    '14':{x:1048,y:170},'15':{x:1105,y:169},'16':{x:1146,y:266},'17':{x:1084,y:265},
    '18':{x:1142,y:588},'19':{x:1202,y:588},'20':{x:1181,y:680},
    '21':{x:1118,y:680},'22':{x:1057,y:680},'23':{x:1046,y:741},'24':{x:1116,y:741},
    '25':{x:1116,y:848},'26':{x:1065,y:892},'27':{x:1005,y:918},
    '28':{x:951,y:870},'29':{x:951,y:807},'30':{x:884,y:794},
    '31':{x:884,y:851},'32':{x:885,y:912},'33':{x:795,y:964},
    '34':{x:793,y:895},'35':{x:794,y:835},'36':{x:728,y:836},
    '37':{x:728,y:896},'38':{x:728,y:963},'39':{x:608,y:882},
    '40':{x:608,y:819},'41':{x:814,y:582}
};

// ── Тост (общий, для ошибок) ────────────────────────────────
function toast(msg, ok){
    var old = document.getElementById('gj-msg-toast');
    if(old) old.remove();
    var t = document.createElement('div');
    t.id = 'gj-msg-toast';
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:399;pointer-events:none;'
        + 'background:var(--mantine-color-body,#1a1b2e);border:1px solid '+(ok?'rgba(74,222,128,.3)':'rgba(239,68,68,.3)')
        + ';border-radius:8px;padding:10px 18px;font-size:13px;font-family:var(--mantine-font-family,inherit);'
        + 'color:'+(ok?'#4ade80':'#f87171')+';display:flex;align-items:center;gap:8px;box-shadow:0 4px 20px rgba(0,0,0,.4);';
    t.innerHTML = '<span>' + msg + '</span>';
    document.body.appendChild(t);
    setTimeout(function(){ t.style.opacity='0'; t.style.transition='opacity .3s'; setTimeout(function(){if(t.parentNode)t.remove();},300); }, 2500);
}

// ── Всплывающее уведомление о новом сообщении (топ-право, 15 сек) ──
function getToastStack(){
    var c = document.getElementById('gj-msg-toast-stack');
    if(!c){
        c = document.createElement('div');
        c.id = 'gj-msg-toast-stack';
        c.style.cssText = 'position:fixed;top:16px;right:16px;z-index:500;display:flex;flex-direction:column;gap:8px;'
            + 'max-width:320px;max-height:'+(MAX_TOASTS_VISIBLE*TOAST_CARD_H)+'px;overflow-y:auto;overflow-x:hidden;'
            + 'pointer-events:auto;scrollbar-width:thin;';
        document.body.appendChild(c);
    }
    return c;
}

function playChime(){
    try{
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        [880, 1175].forEach(function(freq, i){
            var osc = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.frequency.value = freq;
            osc.type = 'sine';
            gain.gain.setValueAtTime(0.001, ctx.currentTime + i*0.09);
            gain.gain.linearRampToValueAtTime(0.12, ctx.currentTime + i*0.09 + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i*0.09 + 0.12);
            osc.connect(gain); gain.connect(ctx.destination);
            osc.start(ctx.currentTime + i*0.09);
            osc.stop(ctx.currentTime + i*0.09 + 0.13);
        });
    }catch(e){}
}

function showMsgToast(pc, text){
    var stack = getToastStack();
    var card = document.createElement('div');
    card.style.cssText = 'background:#221417;border:1px solid rgba(255,255,255,.08);border-left:3px solid #d4172a;'
        + 'border-radius:8px;padding:10px 12px;box-shadow:0 8px 24px rgba(0,0,0,.5);'
        + 'font-family:var(--mantine-font-family,inherit);cursor:pointer;pointer-events:auto;'
        + 'opacity:0;transform:translateX(24px);transition:opacity .2s ease,transform .2s ease;';

    var head = document.createElement('div');
    head.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:4px;';
    var pcLbl = document.createElement('span');
    pcLbl.style.cssText = 'font-size:11px;font-weight:800;color:#ff5252;letter-spacing:.3px;';
    pcLbl.textContent = 'ПК ' + pc;
    var closeX = document.createElement('button');
    closeX.style.cssText = 'background:none;border:none;color:rgba(255,255,255,.4);cursor:pointer;font-size:14px;line-height:1;padding:0;';
    closeX.textContent = '×';
    head.appendChild(pcLbl); head.appendChild(closeX);

    var body = document.createElement('div');
    body.style.cssText = 'font-size:12.5px;color:#eef0f5;word-break:break-word;line-height:1.4;';
    body.textContent = text;

    card.appendChild(head); card.appendChild(body);

    var timer = setTimeout(remove, TOAST_MS);
    function remove(){
        card.style.opacity='0'; card.style.transform='translateX(24px)';
        setTimeout(function(){ if(card.parentNode) card.remove(); }, 200);
    }
    closeX.addEventListener('click', function(e){ e.stopPropagation(); clearTimeout(timer); remove(); });
    card.addEventListener('click', function(){ clearTimeout(timer); remove(); openChat(pc); });

    stack.appendChild(card);
    requestAnimationFrame(function(){ card.style.opacity='1'; card.style.transform='translateX(0)'; });
    playChime();
}

// ── Бейджи непрочитанных (кнопка в сайдбаре + карточки на карте) ──
function updateBadges(){
    var total = 0;
    Object.keys(_unread).forEach(function(pc){ total += (_unread[pc]||0); });

    var dot = document.getElementById('gj-msg-badge');
    if(dot){
        if(total > 0){ dot.style.display='flex'; dot.textContent = total>9 ? '9+' : String(total); }
        else dot.style.display='none';
    }

    if(_popupOpen){
        Object.keys(PC_POS).forEach(function(pc){
            var mark = document.getElementById('gj-msg-dot-'+pc);
            if(!mark) return;
            mark.style.display = (_unread[pc] > 0) ? 'block' : 'none';
        });
    }
}

// Отмечает ПК прочитанным: локально (бейджи) и на сервере (галочки у клиента)
function markReadFor(pc, uptoId){
    if(!uptoId) return;
    if((_readState[pc]||0) >= uptoId && !(_unread[pc]>0)) return;
    _readState[pc] = Math.max(_readState[pc]||0, uptoId);
    saveReadState();
    _unread[pc] = 0;
    updateBadges();
    fetch(PROXY+'/read',{
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({pc:pc, side:'admin', upto:uptoId})
    }).catch(function(){});
}

// ── Глобальный поллинг всех новых сообщений (для тостов/бейджей) ──
function startEventsPoll(){
    fetch(PROXY+'/events?since=0')
        .then(function(r){ return r.json(); })
        .then(function(msgs){
            msgs.forEach(function(m){
                _lastEventId = Math.max(_lastEventId, m.id);
                if(m.from === 'client'){
                    var readUpto = _readState[m.pc] || 0;
                    if(m.id > readUpto) _unread[m.pc] = (_unread[m.pc]||0) + 1;
                }
            });
            updateBadges();
        })
        .catch(function(){});
    if(_eventsTimer) clearInterval(_eventsTimer);
    _eventsTimer = setInterval(pollEvents, POLL_EVENTS_MS);
    refreshNotifySetting();
    setInterval(refreshNotifySetting, 10000);
}

function pollEvents(){
    fetch(PROXY+'/events?since='+_lastEventId)
        .then(function(r){ return r.json(); })
        .then(function(msgs){
            msgs.forEach(function(m){
                _lastEventId = Math.max(_lastEventId, m.id);
                if(m.from !== 'client') return;
                if(_chatPC === m.pc && _chat && document.hasFocus()){
                    // чат с этим ПК открыт и вкладка реально в фокусе — считается прочитанным сразу
                    markReadFor(m.pc, m.id);
                    return;
                }
                if(!_notifyInErp) return; // админ выбрал уведомления в отдельной программе
                _unread[m.pc] = (_unread[m.pc]||0) + 1;
                updateBadges();
                var preview = m.type === 'image' ? '📷 Изображение' : (m.type === 'file' ? '📄 Файл: '+(m.filename||'') : m.text);
                showMsgToast(m.pc, preview);
            });
        })
        .catch(function(){});
}

// ── Карта (попап) ──────────────────────────────────────────
var _popup = null;
var _popupOpen = false;
var _statusData = {};
var _statusTimer = null;

function togglePopup(){
    if(_popupOpen){ closePopup(); return; }
    openPopup();
}

function openPopup(){
    closePopup();
    _popupOpen = true;
    updateSidebarBtn(true);

    var popup = document.createElement('div');
    _popup = popup;
    popup.id = 'gj-msg-popup';
    popup.style.cssText=[
        'position:fixed','left:284px','top:60px',
        'width:'+POPUP_W+'px','max-height:calc(100vh - 70px)',
        'z-index:299',
        'background:#181113',
        'border:1px solid rgba(255,255,255,0.1)',
        'border-radius:0 8px 8px 0',
        'box-shadow:4px 0 32px rgba(0,0,0,.7)',
        'font-family:var(--mantine-font-family,inherit)',
        'overflow:hidden','display:flex','flex-direction:column',
        'transform:translateX(-20px)','opacity:0',
        'transition:transform 0.2s ease,opacity 0.2s ease',
    ].join(';');
    requestAnimationFrame(function(){popup.style.transform='translateX(0)';popup.style.opacity='1';});

    var hdr = document.createElement('div');
    hdr.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:12px 14px 10px;border-bottom:1px solid rgba(255,255,255,0.08);flex-shrink:0;color:#f5eeee;background:#181113;';

    var hdrL = document.createElement('div');
    hdrL.style.cssText = 'display:flex;align-items:center;gap:8px;';
    var hIco = document.createElement('div');
    hIco.style.cssText = 'width:26px;height:26px;background:var(--mantine-color-gg_primary-filled,#d4172a);border-radius:6px;display:flex;align-items:center;justify-content:center;';
    hIco.innerHTML = msgIconSvg('#fff', 13);
    var hTxt = document.createElement('span');
    hTxt.style.cssText = 'font-size:13px;font-weight:700;color:#f5eeee;';
    hTxt.textContent = 'Сообщения';
    hdrL.appendChild(hIco); hdrL.appendChild(hTxt);

    var statusDot = document.createElement('span');
    statusDot.id = 'gj-msg-status-dot';
    statusDot.style.cssText = 'font-size:11px;color:rgba(245,238,238,0.6);font-weight:500;';
    statusDot.textContent = '●  проверка…';

    var closeBtn = document.createElement('button');
    closeBtn.style.cssText = 'background:none;border:none;cursor:pointer;padding:4px;color:#f5eeee;opacity:0.6;font-size:18px;line-height:1;';
    closeBtn.textContent = '×';
    closeBtn.onclick = closePopup;

    hdr.appendChild(hdrL); hdr.appendChild(statusDot); hdr.appendChild(closeBtn);
    popup.appendChild(hdr);

    var legend = document.createElement('div');
    legend.style.cssText = 'display:flex;gap:14px;padding:8px 14px;font-size:11px;color:rgba(245,238,238,.6);border-bottom:1px solid rgba(255,255,255,0.06);';
    legend.innerHTML = '<span><span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:#1565c0;margin-right:5px;"></span>ПК онлайн</span>'
        + '<span><span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:#8a8f98;margin-right:5px;"></span>ПК офлайн</span>';
    popup.appendChild(legend);

    var mapWrap = document.createElement('div');
    mapWrap.style.cssText='position:relative;width:'+POPUP_W+'px;height:'+POPUP_H+'px;flex-shrink:0;overflow:hidden;';
    mapWrap.id = 'gj-msg-map';
    popup.appendChild(mapWrap);

    document.body.appendChild(popup);

    setTimeout(function(){ document.addEventListener('click', outsideClosePopup); }, 0);

    loadStatus(mapWrap, statusDot);
    _statusTimer = setInterval(function(){ loadStatus(mapWrap, statusDot); }, POLL_STATUS_MS);
}

function outsideClosePopup(e){
    if(_popup && !_popup.contains(e.target)){
        var btn = document.getElementById('gj-msg-sidebar-btn');
        if(btn && btn.contains(e.target)) return;
        var chat = document.getElementById('gj-msg-chat');
        if(chat && chat.contains(e.target)) return;
        closePopup();
    }
}

function closePopup(){
    if(_statusTimer){ clearInterval(_statusTimer); _statusTimer=null; }
    if(_popup){
        var p=_popup; _popup=null;
        p.style.transform='translateX(-20px)'; p.style.opacity='0';
        setTimeout(function(){ if(p.parentNode) p.remove(); },220);
    }
    _popupOpen=false; updateSidebarBtn(false);
    document.removeEventListener('click',outsideClosePopup);
}

function loadStatus(mapWrap, statusDot){
    fetch(PROXY + '/status')
        .then(function(r){ return r.json(); })
        .then(function(data){
            console.log('[godji-messenger] /status ответ:', data);
            _statusData = data;
            var online = Object.keys(data).filter(function(k){return data[k].online;}).length;
            statusDot.innerHTML = '<span style="color:#4ade80;">●</span> <span>'+online+' онлайн</span>';
            renderMap(mapWrap, data);
        })
        .catch(function(e){
            console.log('[godji-messenger] /status ошибка:', e);
            statusDot.innerHTML = '<span style="color:#f87171;">●</span> <span>нет сервера</span>';
            renderMap(mapWrap, {});
        });
}

function renderMap(mapWrap, data){
    mapWrap.innerHTML='';
    mapWrap.style.cssText='position:relative;width:'+POPUP_W+'px;height:'+POPUP_H+'px;flex-shrink:0;overflow:hidden;';
    var bgScaleX=(POPUP_W/CROP_W)*(LAYER_W/MAP_ORIG_W);
    var bgScaleY=(POPUP_H/CROP_H)*(LAYER_H/MAP_ORIG_H);
    var bgW=Math.round(MAP_ORIG_W*bgScaleX);
    var bgH=Math.round(MAP_ORIG_H*bgScaleY);
    var bgOffX=-Math.round(CROP_X*(bgW/LAYER_W));
    var bgOffY=-Math.round(CROP_Y*(bgH/LAYER_H));
    var bgWrap=document.createElement('div');
    bgWrap.style.cssText='position:absolute;inset:0;overflow:hidden;pointer-events:none;';
    var img=document.createElement('img');
    img.src=MAP_IMG;
    img.style.cssText='position:absolute;left:'+bgOffX+'px;top:'+bgOffY+'px;width:'+bgW+'px;height:'+bgH+'px;display:block;';
    bgWrap.appendChild(img); mapWrap.appendChild(bgWrap);

    var CARD=36;
    Object.keys(PC_POS).forEach(function(name){
        var pos=PC_POS[name];
        var cx=pos.x+CARD_ORIG/2, cy=pos.y+CARD_ORIG/2;
        var px=Math.round((cx-CROP_X)*MAP_SCALE)-CARD/2;
        var py=Math.round((cy-CROP_Y)*MAP_SCALE)-CARD/2;
        var st=data[name];
        var online=!!(st && st.online);
        var cell=document.createElement('button');
        cell.title='ПК '+name+(online?' — онлайн':' — офлайн');
        var bg = online ? 'linear-gradient(135deg,#1565c0 0%,#1e88e5 100%)' : 'linear-gradient(135deg,#6b6f76 0%,#8a8f98 100%)';
        var bdr = online ? '#0d47a1' : '#555a61';
        cell.style.cssText=[
            'position:absolute',
            'left:'+px+'px','top:'+py+'px',
            'width:'+CARD+'px','height:'+CARD+'px',
            'border-radius:7px',
            'border:2px solid '+bdr,
            'background:'+bg,
            'color:#fff',
            'font-size:8px','font-weight:800',
            'cursor:'+(online?'pointer':'default'),
            'display:flex','flex-direction:column','align-items:center','justify-content:center',
            'gap:2px','font-family:inherit','padding:0','line-height:1',
            'transition:transform .12s,box-shadow .12s','z-index:2',
            'text-shadow:0 1px 3px rgba(0,0,0,0.7)',
            'box-shadow:0 2px 6px rgba(0,0,0,0.35)',
        ].join(';');
        var lbl=document.createElement('span');
        lbl.style.cssText='color:#fff;font-size:8px;font-weight:800;line-height:1;pointer-events:none;';
        lbl.textContent=name; cell.appendChild(lbl);

        var badgeDot=document.createElement('span');
        badgeDot.id='gj-msg-dot-'+name;
        badgeDot.style.cssText='position:absolute;top:-3px;right:-3px;width:10px;height:10px;background:#e03131;'
            + 'border-radius:50%;border:2px solid #181113;z-index:11;display:'+((_unread[name]>0)?'block':'none')+';';
        cell.appendChild(badgeDot);

        if(online){
            cell.addEventListener('mouseenter',function(){ cell.style.transform='scale(1.18)'; cell.style.zIndex='10'; });
            cell.addEventListener('mouseleave',function(){ cell.style.transform=''; cell.style.zIndex='2'; });
            cell.addEventListener('click',function(e){ e.stopPropagation(); openChat(name); });
        }
        mapWrap.appendChild(cell);
    });
}

function msgIconSvg(color, size){
    color = color || 'currentColor'; size = size || 24;
    return '<svg xmlns="http://www.w3.org/2000/svg" width="'+size+'" height="'+size+'" viewBox="0 0 24 24" fill="none" stroke="'+color+'" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        + '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path>'
        + '</svg>';
}

// ── Плавающее окно чата ──────────────────────────────────
var _chat = null;
var _chatPC = null;
var _chatPinned = false;
var _chatTimer = null;
var _chatLastId = 0;
var _chatClearedSeen = 0;

function openChat(pc){
    closeChat();
    _chatPC = pc;
    _chatLastId = 0;
    _chatOwnBubbles = [];
    _chatClearedSeen = 0;
    fetch(PROXY+'/clear_state?pc='+encodeURIComponent(pc)).then(function(r){return r.json();})
        .then(function(d){ _chatClearedSeen = d.clearedAt || 0; }).catch(function(){});

    var win = document.createElement('div');
    _chat = win;
    win.id = 'gj-msg-chat';
    win.style.cssText=[
        'position:fixed','left:'+(300+POPUP_W)+'px','top:80px',
        'width:320px','height:420px','z-index:400',
        'background:#181113','border-radius:10px',
        'box-shadow:0 8px 40px rgba(0,0,0,.5)',
        'font-family:var(--mantine-font-family,inherit)',
        'display:flex','flex-direction:column','overflow:hidden',
        'border:1px solid rgba(255,255,255,0.08)',
    ].join(';');

    var hdr = document.createElement('div');
    hdr.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:10px 12px;background:var(--mantine-color-gg_primary-filled,#d4172a);color:#fff;cursor:move;flex-shrink:0;';
    var hTxt = document.createElement('span');
    hTxt.style.cssText = 'font-size:13px;font-weight:700;';
    hTxt.textContent = 'ПК ' + pc;
    fetch(PROXY+'/nickname?pc='+encodeURIComponent(pc)).then(function(r){return r.json();})
        .then(function(d){ if(d.nickname) hTxt.textContent = d.nickname + ' — ПК ' + pc; })
        .catch(function(){});

    var renameBtn = document.createElement('button');
    renameBtn.title = 'Переименовать';
    renameBtn.style.cssText = 'background:none;border:none;cursor:pointer;padding:0 4px;color:rgba(255,255,255,.75);font-size:12px;line-height:1;';
    renameBtn.textContent = '✎';
    renameBtn.onclick = function(){
        var name = prompt('Ник для ПК ' + pc + ':', hTxt.textContent.split(' — ')[0]);
        if(name && name.trim()){
            fetch(PROXY+'/nickname',{method:'POST',headers:{'Content-Type':'application/json'},
                body:JSON.stringify({pc:pc, nickname:name.trim(), manual:true})})
                .then(function(){ hTxt.textContent = name.trim() + ' — ПК ' + pc; })
                .catch(function(){});
        }
    };
    var hBtns = document.createElement('div');
    hBtns.style.cssText = 'display:flex;align-items:center;gap:6px;';

    var pinBtn = document.createElement('button');
    pinBtn.title = 'Закрепить окно';
    pinBtn.style.cssText = 'background:rgba(255,255,255,.15);border:none;border-radius:5px;cursor:pointer;padding:4px 6px;color:#fff;font-size:12px;line-height:1;';
    pinBtn.textContent = '📌';
    pinBtn.onclick = function(){
        _chatPinned = !_chatPinned;
        pinBtn.style.background = _chatPinned ? 'rgba(255,255,255,.4)' : 'rgba(255,255,255,.15)';
    };

    var closeBtn = document.createElement('button');
    closeBtn.style.cssText = 'background:none;border:none;cursor:pointer;color:#fff;font-size:18px;line-height:1;padding:2px 4px;';
    closeBtn.textContent = '×';
    closeBtn.onclick = closeChat;

    hBtns.appendChild(renameBtn); hBtns.appendChild(pinBtn); hBtns.appendChild(closeBtn);
    hdr.appendChild(hTxt); hdr.appendChild(hBtns);
    win.appendChild(hdr);
    makeDraggable(win, hdr);

    var body = document.createElement('div');
    body.id = 'gj-msg-chat-body';
    body.style.cssText = 'flex:1;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:6px;background:#181113;';
    win.appendChild(body);

    var footer = document.createElement('div');
    footer.style.cssText = 'display:flex;gap:6px;padding:8px;border-top:1px solid rgba(255,255,255,0.08);flex-shrink:0;background:#181113;align-items:center;';

    var fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.style.display = 'none';

    var attachBtn = document.createElement('button');
    attachBtn.type = 'button';
    attachBtn.title = 'Прикрепить файл';
    attachBtn.textContent = '📎';
    attachBtn.style.cssText = 'background:none;border:none;cursor:pointer;font-size:16px;padding:0 4px;color:#f5eeee;opacity:.7;';
    attachBtn.onclick = function(){ fileInput.click(); };

    var input = document.createElement('input');
    input.type = 'text';
    input.placeholder = 'Сообщение…';
    input.style.cssText = 'flex:1;border:1px solid rgba(255,255,255,0.15);background:#241619;color:#f5eeee;border-radius:6px;padding:7px 10px;font-size:13px;outline:none;';
    var sendBtn = document.createElement('button');
    sendBtn.textContent = 'Отправить';
    sendBtn.style.cssText = 'background:var(--mantine-color-gg_primary-filled,#d4172a);color:#fff;border:none;border-radius:6px;padding:0 12px;font-size:12px;font-weight:600;cursor:pointer;';

    function sendImageFile(file){
        var reader = new FileReader();
        reader.onload = function(){
            var b64 = String(reader.result).split(',')[1];
            fetch(PROXY+'/send',{
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({pc:_chatPC, from:'admin', type:'image', text:b64})
            }).then(function(){ pollChat(true); })
              .catch(function(){ toast('Не удалось отправить изображение', false); });
        };
        reader.readAsDataURL(file);
    }
    function sendGenericFile(file){
        var reader = new FileReader();
        reader.onload = function(){
            var b64 = String(reader.result).split(',')[1];
            fetch(PROXY+'/send',{
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({pc:_chatPC, from:'admin', type:'file', filename:file.name, text:b64})
            }).then(function(){ pollChat(true); })
              .catch(function(){ toast('Не удалось отправить файл', false); });
        };
        reader.readAsDataURL(file);
    }
    function sendAnyFile(file){
        if(file.type && file.type.indexOf('image') === 0) sendImageFile(file);
        else sendGenericFile(file);
    }
    fileInput.addEventListener('change', function(){
        if(fileInput.files && fileInput.files[0]) sendAnyFile(fileInput.files[0]);
        fileInput.value = '';
    });
    input.addEventListener('paste', function(e){
        var items = (e.clipboardData || window.clipboardData).items;
        for(var i=0;i<items.length;i++){
            if(items[i].kind === 'file'){
                var file = items[i].getAsFile();
                if(file){ sendAnyFile(file); e.preventDefault(); return; }
            }
        }
    });

    function doSend(silent){
        var text = input.value.trim();
        if(!text) return;
        input.value='';
        fetch(PROXY+'/send',{
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({pc:_chatPC, from:'admin', type:'text', text:text, silent:!!silent})
        }).then(function(){ pollChat(true); })
          .catch(function(){ toast('Нет связи с сервером сообщений', false); });
    }
    sendBtn.onclick = function(){ doSend(false); };
    sendBtn.addEventListener('contextmenu', function(e){
        e.preventDefault();
        showMsgMenu(e.clientX, e.clientY, [
            {label:'🔇  Без звука у клиента', action:function(){ doSend(true); }}
        ]);
    });
    input.addEventListener('keydown', function(e){ if(e.key==='Enter') doSend(false); });

    footer.appendChild(attachBtn); footer.appendChild(fileInput);
    footer.appendChild(input); footer.appendChild(sendBtn);
    win.appendChild(footer);

    document.body.appendChild(win);
    setTimeout(function(){ input.focus(); },50);

    pollChat(true);
    pollReadState();
    pingChatActive();
    _chatTimer = setInterval(function(){ pollChat(false); pollReadState(); pingChatActive(); checkClearState(); }, POLL_CHAT_MS);

    document.addEventListener('click', outsideCloseChat);
}

function outsideCloseChat(e){
    if(_chat && !_chatPinned && !_chat.contains(e.target)){
        var mapCell = e.target.closest && e.target.closest('#gj-msg-map button');
        if(mapCell) return; // клик по карте — не закрываем, обработает свой хендлер
        closeChat();
    }
}

// ── Контекстное меню сообщения (ПКМ): скопировать текст/картинку, сохранить ──
function closeMsgMenu(){
    var m = document.getElementById('gj-msg-ctxmenu');
    if(m) m.remove();
    document.removeEventListener('click', closeMsgMenu);
}

function showMsgMenu(x, y, items){
    closeMsgMenu();
    var menu = document.createElement('div');
    menu.id = 'gj-msg-ctxmenu';
    menu.style.cssText = 'position:fixed;left:'+x+'px;top:'+y+'px;z-index:600;background:#221417;'
        + 'border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:6px;min-width:180px;'
        + 'box-shadow:0 8px 24px rgba(0,0,0,.5);font-family:var(--mantine-font-family,inherit);';
    items.forEach(function(it){
        var row = document.createElement('div');
        row.textContent = it.label;
        row.style.cssText = 'padding:8px 10px;font-size:12.5px;color:#f5eeee;border-radius:5px;cursor:pointer;';
        row.addEventListener('mouseenter', function(){ row.style.background = '#d4172a'; });
        row.addEventListener('mouseleave', function(){ row.style.background = 'transparent'; });
        row.addEventListener('click', function(e){ e.stopPropagation(); closeMsgMenu(); it.action(); });
        menu.appendChild(row);
    });
    document.body.appendChild(menu);
    setTimeout(function(){ document.addEventListener('click', closeMsgMenu); }, 0);
}

function attachMsgContextMenu(el, opts){
    el.addEventListener('contextmenu', function(e){
        e.preventDefault();
        var items = [];
        if(opts.text){
            items.push({label:'Скопировать текст', action:function(){
                navigator.clipboard && navigator.clipboard.writeText(opts.text).catch(function(){});
            }});
        }
        if(opts.imgSrc){
            items.push({label:'Скопировать изображение', action:function(){
                fetch(opts.imgSrc).then(function(r){return r.blob();}).then(function(blob){
                    if(navigator.clipboard && window.ClipboardItem){
                        navigator.clipboard.write([new ClipboardItem({[blob.type]: blob})]).catch(function(){});
                    }
                }).catch(function(){});
            }});
            items.push({label:'Сохранить как…', action:function(){
                var a = document.createElement('a');
                a.href = opts.imgSrc; a.download = 'godji-image.jpg';
                document.body.appendChild(a); a.click(); a.remove();
            }});
        }
        showMsgMenu(e.clientX, e.clientY, items);
    });
}

function pollChat(scrollToEnd){
    if(!_chat || !_chatPC) return;
    fetch(PROXY+'/messages?pc='+encodeURIComponent(_chatPC)+'&since='+_chatLastId)
        .then(function(r){return r.json();})
        .then(function(msgs){
            var body = document.getElementById('gj-msg-chat-body');
            if(!body) return;
            msgs.forEach(function(m){
                _chatLastId = Math.max(_chatLastId, m.id);
                var mine = m.from === 'admin';

                var row = document.createElement('div');
                row.style.cssText = 'align-self:'+(mine?'flex-end':'flex-start')+';max-width:80%;display:flex;flex-direction:column;'+(mine?'align-items:flex-end;':'align-items:flex-start;');

                var bubble = document.createElement('div');
                if(m.type === 'image'){
                    bubble.style.cssText = 'background:'+(mine?'var(--mantine-color-gg_primary-filled,#d4172a)':'#2b1c1f')+';padding:4px;border-radius:10px;cursor:pointer;';
                    var img = document.createElement('img');
                    img.src = 'data:image/jpeg;base64,' + m.text;
                    img.style.cssText = 'max-width:200px;max-height:200px;display:block;border-radius:6px;';
                    img.onclick = function(src){ return function(){ window.open(src,'_blank'); }; }(img.src);
                    bubble.appendChild(img);
                    attachMsgContextMenu(bubble, {imgSrc: img.src});
                } else if(m.type === 'file'){
                    bubble.style.cssText = 'background:'+(mine?'var(--mantine-color-gg_primary-filled,#d4172a)':'#2b1c1f')+';padding:10px 12px;border-radius:10px;cursor:pointer;display:flex;align-items:center;gap:8px;max-width:210px;';
                    var icon = document.createElement('span'); icon.textContent = '📄'; icon.style.cssText='font-size:16px;';
                    var nameSpan = document.createElement('span');
                    nameSpan.style.cssText = 'font-size:12.5px;color:'+(mine?'#fff':'#f5eeee')+';overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
                    nameSpan.textContent = m.filename || 'файл';
                    bubble.appendChild(icon); bubble.appendChild(nameSpan);
                    bubble.addEventListener('click', function(fname, b64){ return function(){
                        var a = document.createElement('a');
                        a.href = 'data:application/octet-stream;base64,' + b64;
                        a.download = fname; document.body.appendChild(a); a.click(); a.remove();
                    }; }(m.filename || 'file', m.text));
                } else {
                    bubble.style.cssText = 'background:'+(mine?'var(--mantine-color-gg_primary-filled,#d4172a)':'#2b1c1f')+';color:'+(mine?'#fff':'#f5eeee')+';padding:6px 10px;border-radius:10px;font-size:13px;word-break:break-word;';
                    bubble.textContent = m.text;
                    attachMsgContextMenu(bubble, {text: m.text});
                }
                row.appendChild(bubble);

                var meta = document.createElement('div');
                meta.style.cssText = 'font-size:9px;color:rgba(245,238,238,0.45);margin-top:2px;padding:0 2px;';
                var ts = new Date(m.ts*1000);
                var hh = ('0'+ts.getHours()).slice(-2), mm=('0'+ts.getMinutes()).slice(-2);
                meta.textContent = hh+':'+mm + (mine ? ' ✓' : '');
                row.appendChild(meta);

                if(mine) _chatOwnBubbles.push({id:m.id, el:meta, ts:hh+':'+mm});

                body.appendChild(row);
            });
            if(msgs.length) body.scrollTop = body.scrollHeight;
            else if(scrollToEnd) body.scrollTop = body.scrollHeight;

            // Отмечаем прочитанным только если вкладка реально в фокусе —
            // иначе галочки ставились, даже когда никто не смотрел в чат
            if(_chatLastId > 0 && document.hasFocus()) markReadFor(_chatPC, _chatLastId);
        })
        .catch(function(){});
}

function pingChatActive(){
    if(!_chatPC || !document.hasFocus()) return;
    fetch(PROXY+'/chat_active',{
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({pc:_chatPC})
    }).catch(function(){});
}

function checkClearState(){
    if(!_chatPC) return;
    fetch(PROXY+'/clear_state?pc='+encodeURIComponent(_chatPC))
        .then(function(r){ return r.json(); })
        .then(function(d){
            var clearedAt = d.clearedAt || 0;
            if(clearedAt > _chatClearedSeen){
                _chatClearedSeen = clearedAt;
                _chatLastId = 0;
                var body = document.getElementById('gj-msg-chat-body');
                if(body) body.innerHTML = '';
                _chatOwnBubbles = [];
            }
        })
        .catch(function(){});
}

function pollReadState(){
    if(!_chat || !_chatPC) return;
    fetch(PROXY+'/read_state?pc='+encodeURIComponent(_chatPC))
        .then(function(r){return r.json();})
        .then(function(data){
            var readClient = data.readClient || 0;
            _chatOwnBubbles.forEach(function(b){
                if(b.id <= readClient){
                    b.el.textContent = b.ts + ' ✓✓';
                    b.el.style.color = '#f2a33c';
                } else {
                    b.el.textContent = b.ts + ' ✓';
                    b.el.style.color = 'rgba(245,238,238,0.45)';
                }
            });
        })
        .catch(function(){});
}

function closeChat(){
    if(_chatTimer){ clearInterval(_chatTimer); _chatTimer=null; }
    if(_chat){ _chat.remove(); _chat=null; }
    _chatPC = null; _chatPinned = false; _chatOwnBubbles = [];
    document.removeEventListener('click', outsideCloseChat);
}

function makeDraggable(win, handle){
    var sx=0, sy=0, ox=0, oy=0, dragging=false;
    handle.addEventListener('mousedown', function(e){
        dragging = true;
        sx = e.clientX; sy = e.clientY;
        var r = win.getBoundingClientRect();
        ox = r.left; oy = r.top;
        e.preventDefault();
    });
    document.addEventListener('mousemove', function(e){
        if(!dragging) return;
        win.style.left = (ox + (e.clientX - sx)) + 'px';
        win.style.top = (oy + (e.clientY - sy)) + 'px';
    });
    document.addEventListener('mouseup', function(){ dragging = false; });
}

// ── Кнопка в футере сайдбара (иконка без текста, возле "Гоголя Админ") ──
function createSidebarBtn(){
    if(document.getElementById('gj-msg-sidebar-btn')) return;
    var footer = document.querySelector('.Sidebar_footer__1BA98');
    if(!footer) return;
    if(getComputedStyle(footer).position === 'static') footer.style.position = 'relative';

    var btn = document.createElement('button');
    btn.id = 'gj-msg-sidebar-btn';
    btn.type = 'button';
    btn.title = 'Сообщения';
    btn.innerHTML = msgIconSvg('#fff', 16);

    // Смещаем левее, если уже есть кнопка настроек (шестерёнка) в том же месте
    var settingsBtn = document.getElementById('godji-settings-btn');
    var rightOffset = settingsBtn ? 56 : 16;

    var s = btn.style;
    s.setProperty('position','absolute');
    s.setProperty('top','50%');
    s.setProperty('right', rightOffset+'px');
    s.setProperty('transform','translateY(-50%)');
    s.setProperty('width','28px');
    s.setProperty('height','28px');
    s.setProperty('border-radius','8px');
    s.setProperty('border','none');
    s.setProperty('background','rgba(255,255,255,0.08)');
    s.setProperty('display','flex');
    s.setProperty('align-items','center');
    s.setProperty('justify-content','center');
    s.setProperty('cursor','pointer');
    s.setProperty('z-index','5');
    btn.addEventListener('mouseenter', function(){ btn.style.background='rgba(255,255,255,0.16)'; });
    btn.addEventListener('mouseleave', function(){ btn.style.background='rgba(255,255,255,0.08)'; });
    btn.addEventListener('click', function(e){ e.stopPropagation(); togglePopup(); });

    var badge = document.createElement('span');
    badge.id = 'gj-msg-badge';
    badge.style.cssText = 'position:absolute;top:-4px;right:-4px;min-width:15px;height:15px;padding:0 3px;'
        + 'background:#e03131;border-radius:8px;display:none;align-items:center;justify-content:center;'
        + 'font-size:9px;font-weight:800;color:#fff;line-height:1;box-shadow:0 0 0 2px rgba(22,23,41,0.9);pointer-events:none;';
    btn.appendChild(badge);

    footer.appendChild(btn);
    updateBadges();
}

function updateSidebarBtn(open){
    var btn = document.getElementById('gj-msg-sidebar-btn');
    if(!btn) return;
    btn.style.background = open ? 'rgba(255,255,255,0.25)' : 'rgba(255,255,255,0.08)';
}

// ── Init ───────────────────────────────────────────────────
function tryInit(){
    if(!document.querySelector('.Sidebar_footer__1BA98')){ setTimeout(tryInit,500); return; }
    createSidebarBtn();
}

new MutationObserver(function(muts){
    muts.forEach(function(m){
        if(m.addedNodes.length && !document.getElementById('gj-msg-sidebar-btn')) tryInit();
    });
}).observe(document.body || document.documentElement, {childList:true, subtree:false});

setTimeout(tryInit, 1000);
setTimeout(tryInit, 2500);
setTimeout(tryInit, 5000);

startEventsPoll();

})();
