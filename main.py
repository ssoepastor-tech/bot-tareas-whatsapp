import os
import json
import re
import httpx
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from flask import Flask, request, Response
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import firebase_admin
from firebase_admin import credentials, firestore
 
app = Flask(__name__)
 
# ── CONFIGURACIÓN ─────────────────────────────────────────
# Las claves se configuran en Railway como variables de entorno
CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')
TWILIO_SID   = os.environ.get('TWILIO_SID')
TWILIO_TOKEN = os.environ.get('TWILIO_TOKEN')
TWILIO_FROM  = 'whatsapp:+14155238886'
EDINSON_WA   = 'whatsapp:+51955428896'
LIMA_TZ      = pytz.timezone('America/Lima')
 
# Firebase — la clave se pega en Railway como variable FIREBASE_JSON
firebase_json = os.environ.get('FIREBASE_JSON', '{}')
cred = credentials.Certificate(json.loads(firebase_json))
firebase_admin.initialize_app(cred)
db = firestore.client()
 
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
 
# ── HELPERS ───────────────────────────────────────────────
def get_tasks():
    docs = db.collection('tareas').stream()
    return [{'id': d.id, **d.to_dict()} for d in docs]
 
def get_priority_label(t):
    u = t.get('urgencia', 3)
    i = t.get('importancia', 3)
    if u >= 4 and i >= 4: return 'URGENTE'
    if u >= 4 or i >= 4:  return 'Alta'
    if u >= 2 or i >= 2:  return 'Media'
    return 'Baja'
 
def get_priority_score(t):
    imp = (t.get('importancia', 3) / 5) * 0.35
    urg = (t.get('urgencia', 3)    / 5) * 0.35
    fac = ((6 - t.get('dificultad', 3)) / 5) * 0.15
    tiempo_map = {'rapida': 10, 'corta': 7, 'media': 4, 'larga': 1}
    efi = (tiempo_map.get(t.get('tiempo', 'media'), 4) / 10) * 0.15
    return round((imp + urg + fac + efi) * 10, 1)
 
def is_overdue(fecha_str):
    if not fecha_str: return False
    try:
        fecha = datetime.strptime(fecha_str, '%Y-%m-%d')
        return fecha.date() < datetime.now().date()
    except:
        return False
 
def days_overdue(fecha_str):
    if not fecha_str: return 0
    try:
        fecha = datetime.strptime(fecha_str, '%Y-%m-%d')
        diff = datetime.now().date() - fecha.date()
        return diff.days if diff.days > 0 else 0
    except:
        return 0
 
def fmt_date(fecha_str):
    if not fecha_str: return 'Sin fecha'
    try:
        d = datetime.strptime(fecha_str, '%Y-%m-%d')
        return d.strftime('%d/%m/%Y')
    except:
        return fecha_str
 
# ── COMANDOS ─────────────────────────────────────────────
def cmd_hoy(tasks):
    hoy = datetime.now().date().isoformat()
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente']
    hoy_tasks  = [t for t in pendientes if t.get('fecha') == hoy]
    vencidas   = [t for t in pendientes if is_overdue(t.get('fecha', ''))]
    
    msg = f"📋 *Resumen de hoy — {datetime.now().strftime('%d/%m/%Y')}*\n\n"
    
    if vencidas:
        msg += f"⚠️ *{len(vencidas)} VENCIDAS:*\n"
        for t in sorted(vencidas, key=lambda x: -get_priority_score(x))[:3]:
            dias = days_overdue(t.get('fecha'))
            msg += f"  • {t.get('titulo')} ({dias}d retraso)\n"
        msg += "\n"
    
    if hoy_tasks:
        msg += f"📅 *{len(hoy_tasks)} para hoy:*\n"
        for t in sorted(hoy_tasks, key=lambda x: -get_priority_score(x))[:5]:
            msg += f"  • {t.get('titulo')} — {get_priority_label(t)}\n"
    elif not vencidas:
        msg += "✅ No tienes tareas para hoy\n"
    
    total_pend = len(pendientes)
    msg += f"\n📊 Total pendientes: {total_pend}"
    return msg
 
def cmd_pendientes(tasks):
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente']
    if not pendientes:
        return "✅ No tienes tareas pendientes. ¡Todo al día!"
    
    sorted_tasks = sorted(pendientes, key=lambda x: -get_priority_score(x))[:8]
    msg = f"📋 *Top tareas pendientes ({len(pendientes)} total):*\n\n"
    
    for i, t in enumerate(sorted_tasks, 1):
        over = is_overdue(t.get('fecha', ''))
        dias = days_overdue(t.get('fecha', ''))
        estado_icon = "⚠️" if over else "🔵"
        retraso = f" ({dias}d retraso)" if over else ""
        fecha = f" | {fmt_date(t.get('fecha'))}" if t.get('fecha') else ""
        msg += f"{estado_icon} *{i}.* {t.get('titulo')}\n"
        msg += f"    {get_priority_label(t)} | ⚡{get_priority_score(t)}pts{fecha}{retraso}\n\n"
    
    return msg.strip()
 
def cmd_quickwins(tasks):
    qw = [t for t in tasks 
          if t.get('estado') == 'pendiente'
          and t.get('tiempo') in ['rapida', 'corta']
          and t.get('dificultad', 5) <= 2]
    
    if not qw:
        return "⚡ No tienes Quick Wins disponibles ahora.\n\nAgrega tareas rápidas y fáciles para verlas aquí."
    
    msg = f"⚡ *Quick Wins — {len(qw)} tareas rápidas:*\n\n"
    for t in sorted(qw, key=lambda x: -get_priority_score(x)):
        tiempo_txt = '< 15min' if t.get('tiempo') == 'rapida' else '15-45min'
        msg += f"• {t.get('titulo')} ({tiempo_txt})\n"
    
    msg += "\n_Cierra estas primero para ganar momentum_ 💪"
    return msg
 
def cmd_nueva_tarea(texto, numero_wa):
    """
    Formato: nueva: [titulo] | imp:[1-5] urg:[1-5] dif:[1-5] fecha:[DD/MM] tiempo:[rapida/corta/media/larga]
    Ejemplo: nueva: Revisar SCTR | imp:5 urg:5 dif:1 fecha:10/04 tiempo:rapida
    """
    texto = texto.replace('nueva:', '').replace('nueva tarea:', '').strip()
    
    # Extraer título (antes del |)
    partes = texto.split('|')
    titulo = partes[0].strip()
    if not titulo:
        return "❌ Falta el título. Ejemplo:\n*nueva: Revisar SCTR | imp:5 urg:4 dif:1 tiempo:rapida*"
    
    # Valores por defecto
    data = {
        'titulo':      titulo,
        'descripcion': '',
        'importancia': 3,
        'urgencia':    3,
        'dificultad':  3,
        'tiempo':      'media',
        'categoria':   'trabajo',
        'estado':      'pendiente',
        'fecha':       '',
        'creadoEl':    datetime.now().date().isoformat(),
        'completadoEl': '',
        'origen':      'whatsapp'
    }
    
    if len(partes) > 1:
        params = partes[1].strip()
        
        m = re.search(r'imp:(\d)', params)
        if m: data['importancia'] = min(5, max(1, int(m.group(1))))
        
        m = re.search(r'urg:(\d)', params)
        if m: data['urgencia'] = min(5, max(1, int(m.group(1))))
        
        m = re.search(r'dif:(\d)', params)
        if m: data['dificultad'] = min(5, max(1, int(m.group(1))))
        
        m = re.search(r'tiempo:(rapida|corta|media|larga)', params)
        if m: data['tiempo'] = m.group(1)
        
        m = re.search(r'fecha:(\d{1,2})[\/\-](\d{1,2})', params)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = datetime.now().year
            if month < datetime.now().month:
                year += 1
            try:
                data['fecha'] = f"{year}-{month:02d}-{day:02d}"
            except:
                pass
    
    # Guardar en Firebase
    db.collection('tareas').add(data)
    
    score = get_priority_score(data)
    tiempo_txt = {'rapida':'< 15min','corta':'15-45min','media':'1-2h','larga':'> 2h'}.get(data['tiempo'],'')
    fecha_txt = fmt_date(data['fecha']) if data['fecha'] else 'Sin fecha'
    
    return (f"✅ *Tarea creada desde WhatsApp*\n\n"
            f"📌 *{titulo}*\n"
            f"Importancia: {'⭐'*data['importancia']}\n"
            f"Urgencia: {'🔴'*data['urgencia']}\n"
            f"Dificultad: {'💪'*data['dificultad']}\n"
            f"Tiempo: {tiempo_txt}\n"
            f"Fecha: {fecha_txt}\n"
            f"Prioridad: ⚡{score} pts")
 
def cmd_completar(texto, tasks):
    # Buscar por número o por texto
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente']
    
    m = re.search(r'completar\s+(\d+)', texto)
    if m:
        idx = int(m.group(1)) - 1
        sorted_tasks = sorted(pendientes, key=lambda x: -get_priority_score(x))
        if 0 <= idx < len(sorted_tasks):
            t = sorted_tasks[idx]
            db.collection('tareas').document(t['id']).update({
                'estado': 'completada',
                'completadoEl': datetime.now().date().isoformat()
            })
            return f"✅ *Completada:* {t.get('titulo')}\n\n¡Excelente trabajo! 🎯"
    
    return "❌ Indica el número. Ejemplo: *completar 2*\nUsa *pendientes* para ver la lista numerada."
 
def cmd_vencidas(tasks):
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente']
    vencidas   = [t for t in pendientes if is_overdue(t.get('fecha', ''))]
    
    if not vencidas:
        return "✅ No tienes tareas vencidas. ¡Todo al día!"
    
    msg = f"⚠️ *{len(vencidas)} tareas vencidas:*\n\n"
    for t in sorted(vencidas, key=lambda x: -days_overdue(x.get('fecha', ''))):
        dias = days_overdue(t.get('fecha', ''))
        msg += f"🔴 *{t.get('titulo')}*\n"
        msg += f"    Vencida hace {dias} día{'s' if dias!=1 else ''} ({fmt_date(t.get('fecha'))})\n\n"
    
    return msg.strip()
 
def cmd_ayuda():
    return """🤖 *Comandos disponibles:*
 
📋 *hoy* — Resumen del día
📋 *pendientes* — Lista de tareas pendientes
⚡ *quickwin* — Tareas rápidas y fáciles
⚠️ *vencidas* — Tareas atrasadas
✅ *completar N* — Marcar tarea N como completada
 
➕ *Crear tarea:*
`nueva: [título] | imp:5 urg:4 dif:1 tiempo:rapida fecha:10/04`
 
_imp/urg/dif = del 1 al 5_
_tiempo = rapida/corta/media/larga_
 
Ejemplo:
`nueva: Revisar SCTR | imp:5 urg:5 dif:1 tiempo:rapida`"""
 
# ── ENVÍO PROACTIVO ──────────────────────────────────────
def send_to_edinson(msg):
    try:
        twilio_client.messages.create(
            from_=TWILIO_FROM,
            to=EDINSON_WA,
            body=msg
        )
        print(f"Mensaje enviado: {msg[:50]}...")
    except Exception as e:
        print(f"Error enviando mensaje: {e}")
 
# ── RECORDATORIOS AUTOMÁTICOS ─────────────────────────────
 
def reminder_manana():
    """7:30 AM Lima — Resumen matutino del día"""
    tasks = get_tasks()
    hoy = datetime.now(LIMA_TZ).date().isoformat()
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente']
    hoy_tasks  = sorted([t for t in pendientes if t.get('fecha') == hoy],
                        key=lambda x: -get_priority_score(x))
    vencidas   = [t for t in pendientes if is_overdue(t.get('fecha', ''))]
 
    msg = f"☀️ *Buenos días Edinson!*\n"
    msg += f"_{datetime.now(LIMA_TZ).strftime('%A %d de %B')}_\n\n"
 
    if vencidas:
        msg += f"⚠️ *{len(vencidas)} tareas vencidas pendientes*\n"
        for t in sorted(vencidas, key=lambda x: -days_overdue(x.get('fecha','')))[:3]:
            dias = days_overdue(t.get('fecha',''))
            msg += f"  🔴 {t.get('titulo')} ({dias}d retraso)\n"
        msg += "\n"
 
    if hoy_tasks:
        msg += f"📅 *{len(hoy_tasks)} tareas para hoy:*\n"
        for t in hoy_tasks[:5]:
            msg += f"  • {t.get('titulo')} — {get_priority_label(t)}\n"
    else:
        msg += "✅ No tienes tareas programadas para hoy\n"
 
    # Quick wins del día
    qw = [t for t in pendientes if t.get('tiempo') in ['rapida','corta'] and t.get('dificultad',5) <= 2]
    if qw:
        msg += f"\n⚡ *{len(qw)} Quick Win{'s' if len(qw)>1 else ''} disponible{'s' if len(qw)>1 else ''}*\n"
        for t in qw[:2]:
            msg += f"  • {t.get('titulo')}\n"
 
    msg += f"\n📊 Total pendientes: {len(pendientes)}"
    msg += "\n\n_Responde *hoy*, *pendientes* o escríbeme lo que necesites agendar_ 💪"
    send_to_edinson(msg)
 
def reminder_nocturno():
    """9:00 PM Lima — Resumen nocturno y planificación mañana"""
    tasks = get_tasks()
    hoy = datetime.now(LIMA_TZ).date().isoformat()
    manana = (datetime.now(LIMA_TZ).date() + timedelta(days=1)).isoformat()
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente']
    completadas_hoy = [t for t in tasks if t.get('estado') == 'completada' and t.get('completadoEl') == hoy]
    manana_tasks = sorted([t for t in pendientes if t.get('fecha') == manana],
                          key=lambda x: -get_priority_score(x))
    vencidas = [t for t in pendientes if is_overdue(t.get('fecha', ''))]
 
    msg = f"🌙 *Resumen nocturno — {datetime.now(LIMA_TZ).strftime('%d/%m/%Y')}*\n\n"
 
    if completadas_hoy:
        msg += f"✅ *Completaste hoy: {len(completadas_hoy)} tarea{'s' if len(completadas_hoy)>1 else ''}*\n"
        for t in completadas_hoy[:3]:
            msg += f"  • {t.get('titulo')}\n"
        msg += "\n"
 
    if vencidas:
        msg += f"⚠️ *{len(vencidas)} vencida{'s' if len(vencidas)>1 else ''} sin cerrar*\n"
        for t in sorted(vencidas, key=lambda x: -get_priority_score(x))[:3]:
            msg += f"  🔴 {t.get('titulo')}\n"
        msg += "\n"
 
    if manana_tasks:
        msg += f"📅 *Mañana tienes {len(manana_tasks)} tarea{'s' if len(manana_tasks)>1 else ''}:*\n"
        for t in manana_tasks[:4]:
            msg += f"  • {t.get('titulo')} — {get_priority_label(t)}\n"
    else:
        msg += "📅 No tienes tareas programadas para mañana\n"
 
    msg += f"\n📊 Pendientes totales: {len(pendientes)}"
    msg += "\n\n_Descansa bien_ 🌟"
    send_to_edinson(msg)
 
def reminder_seguimiento():
    """2:00 PM Lima — Seguimiento de tareas delegadas y en curso"""
    tasks = get_tasks()
    hoy = datetime.now(LIMA_TZ).date().isoformat()
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente']
    hoy_tasks = sorted([t for t in pendientes if t.get('fecha') == hoy],
                       key=lambda x: -get_priority_score(x))
    vencidas = [t for t in pendientes if is_overdue(t.get('fecha', ''))]
 
    completadas_hoy = [t for t in tasks if t.get('estado') == 'completada' and t.get('completadoEl') == hoy]
    total_hoy = len(hoy_tasks) + len(completadas_hoy)
    avance = f"{len(completadas_hoy)}/{total_hoy}" if total_hoy > 0 else "0/0"
 
    msg = f"📋 *Seguimiento de tarde — {datetime.now(LIMA_TZ).strftime('%d/%m/%Y')}*\n"
    msg += f"_Son las 2:00 PM — quedan 3h 45min para la salida_\n\n"
    msg += f"Avance del día: *{avance} tareas* completadas\n\n"
 
    if hoy_tasks:
        msg += f"🔵 *Pendientes de hoy:*\n"
        for t in hoy_tasks[:4]:
            msg += f"  • {t.get('titulo')} ⚡{get_priority_score(t)}pts\n"
        msg += "\n"
 
    if vencidas:
        msg += f"⚠️ *{len(vencidas)} vencida{'s' if len(vencidas)>1 else ''} — resolver hoy:*\n"
        for t in sorted(vencidas, key=lambda x: -get_priority_score(x))[:3]:
            dias = days_overdue(t.get('fecha',''))
            msg += f"  🔴 {t.get('titulo')} ({dias}d)\n"
 
    msg += "\n_¿Qué vas a cerrar antes de salir?_ 💪"
    send_to_edinson(msg)
 
def reminder_vencimientos():
    """Se ejecuta cada mañana — alerta 7 y 3 días hábiles antes del vencimiento"""
    tasks = get_tasks()
    hoy = datetime.now(LIMA_TZ).date()
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente' and t.get('fecha')]
 
    alertas_7 = []
    alertas_3 = []
 
    for t in pendientes:
        try:
            fecha = datetime.strptime(t['fecha'], '%Y-%m-%d').date()
            # Calcular días hábiles restantes (lunes-viernes)
            dias_habiles = 0
            current = hoy
            while current < fecha:
                current += timedelta(days=1)
                if current.weekday() < 5:  # lunes=0 a viernes=4
                    dias_habiles += 1
            if dias_habiles == 7:
                alertas_7.append(t)
            elif dias_habiles == 3:
                alertas_3.append(t)
        except:
            pass
 
    if alertas_7:
        msg = f"📅 *Alerta — 7 días hábiles*\n"
        msg += f"Las siguientes tareas vencen en exactamente 7 días hábiles:\n\n"
        for t in alertas_7:
            msg += f"  🟡 *{t.get('titulo')}*\n"
            msg += f"     Vence: {fmt_date(t.get('fecha'))} | {get_priority_label(t)}\n"
        msg += "\n_Planifica con anticipación_ ⏰"
        send_to_edinson(msg)
 
    if alertas_3:
        msg = f"🔔 *Alerta — 3 días hábiles*\n"
        msg += f"¡Atención! Estas tareas vencen en 3 días hábiles:\n\n"
        for t in alertas_3:
            msg += f"  🟠 *{t.get('titulo')}*\n"
            msg += f"     Vence: {fmt_date(t.get('fecha'))} | {get_priority_label(t)}\n"
        msg += "\n_Es momento de priorizar_ ⚠️"
        send_to_edinson(msg)
 
# ── INICIAR SCHEDULER ─────────────────────────────────────
scheduler = BackgroundScheduler(timezone=LIMA_TZ)
# 7:30 AM Lima — Buenos días + resumen matutino
scheduler.add_job(reminder_manana,     CronTrigger(hour=7,  minute=30, timezone=LIMA_TZ))
# 9:00 PM Lima — Resumen nocturno
scheduler.add_job(reminder_nocturno,   CronTrigger(hour=21, minute=0,  timezone=LIMA_TZ))
# 2:00 PM Lima — Seguimiento de tarde
scheduler.add_job(reminder_seguimiento,CronTrigger(hour=14, minute=0,  timezone=LIMA_TZ))
# 7:35 AM Lima — Alertas de vencimiento 7 y 3 días hábiles
scheduler.add_job(reminder_vencimientos,CronTrigger(hour=7, minute=35, timezone=LIMA_TZ))
scheduler.start()
print("Scheduler iniciado — recordatorios activos para Lima (UTC-5)")
 
# ── PROCESADOR DE LENGUAJE NATURAL CON CLAUDE ────────────
def procesar_con_claude(mensaje, tasks):
    hoy = datetime.now()
    contexto_tareas = ""
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente'][:5]
    if pendientes:
        contexto_tareas = "\nTareas pendientes actuales: " + ", ".join([t.get('titulo','') for t in pendientes])
    
    prompt = f"""Eres el asistente del gestor de tareas de Edinson Pastor, profesional SSOMA en Perú.
Hoy es {hoy.strftime('%A %d de %B de %Y')}.
Jornada laboral: 7:30 AM - 5:45 PM, almuerzo 12:00-2:00 PM.{contexto_tareas}
 
El usuario envió: "{mensaje}"
 
Analiza el mensaje y responde SOLO con un JSON válido sin texto adicional:
 
Si es para CREAR una tarea:
{{"accion": "crear", "titulo": "título claro y conciso", "fecha": "YYYY-MM-DD o null", "importancia": 1-5, "urgencia": 1-5, "dificultad": 1-5, "tiempo": "rapida/corta/media/larga", "categoria": "trabajo/personal/estudio/salud"}}
 
Si es para CONSULTAR tareas (hoy, pendientes, vencidas, quickwin):
{{"accion": "consultar", "tipo": "hoy/pendientes/vencidas/quickwin"}}
 
Si es para COMPLETAR una tarea:
{{"accion": "completar", "numero": N}}
 
Si es SALUDO o pregunta general:
{{"accion": "ayuda"}}
 
Reglas para fechas:
- "hoy" = {hoy.strftime('%Y-%m-%d')}
- "mañana" = {(hoy + timedelta(days=1)).strftime('%Y-%m-%d')}
- "lunes próximo" = calcula la fecha del próximo lunes
- "esta semana" = viernes de esta semana
- Sin fecha mencionada = null
 
Reglas para importancia/urgencia (1-5):
- "urgente", "crítico", "ya" = 5
- "importante", "prioridad" = 4
- normal = 3
- "cuando pueda", "después" = 2
- "algún día" = 1
 
Reglas para tiempo:
- "rápido", "5 min", "momento" = rapida
- "media hora", "reunión corta" = corta  
- "reunión", "presentación", "informe" = media
- "proyecto", "auditoría", "capacitación" = larga"""
 
    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=10.0
        )
        result = response.json()
        text = result['content'][0]['text'].strip()
        # Limpiar por si Claude agrega texto extra
        if '{' in text:
            text = text[text.index('{'):text.rindex('}')+1]
        return json.loads(text)
    except Exception as e:
        print(f"Error Claude: {e}")
        return None
 
# ── WEBHOOK PRINCIPAL ─────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    incoming = request.form.get('Body', '').strip().lower()
    sender   = request.form.get('From', '')
    
    tasks = get_tasks()
    body_original = request.form.get('Body', '').strip()
    
    # Comandos directos rápidos (sin gastar API)
    if any(x in incoming for x in ['hoy', 'resumen', 'buenos días', 'buenos dias']):
        reply = cmd_hoy(tasks)
    elif incoming in ['pendientes', 'lista', 'tareas']:
        reply = cmd_pendientes(tasks)
    elif incoming in ['quickwin', 'quick win', 'quickwins']:
        reply = cmd_quickwins(tasks)
    elif incoming in ['vencidas', 'atrasadas']:
        reply = cmd_vencidas(tasks)
    elif incoming in ['ayuda', 'help', 'comandos', 'hola', 'menu']:
        reply = cmd_ayuda()
    elif re.match(r'^completar\s+\d+$', incoming):
        reply = cmd_completar(incoming, tasks)
    elif incoming.startswith('nueva:'):
        reply = cmd_nueva_tarea(body_original, sender)
    else:
        # Lenguaje natural con Claude AI
        interpretacion = procesar_con_claude(body_original, tasks)
        
        if interpretacion is None:
            reply = cmd_ayuda()
        elif interpretacion['accion'] == 'crear':
            # Construir tarea desde interpretación
            data = {
                'titulo':      interpretacion.get('titulo', body_original[:50]),
                'descripcion': '',
                'importancia': interpretacion.get('importancia', 3),
                'urgencia':    interpretacion.get('urgencia', 3),
                'dificultad':  interpretacion.get('dificultad', 3),
                'tiempo':      interpretacion.get('tiempo', 'media'),
                'categoria':   interpretacion.get('categoria', 'trabajo'),
                'fecha':       interpretacion.get('fecha') or '',
                'estado':      'pendiente',
                'creadoEl':    datetime.now().date().isoformat(),
                'completadoEl': '',
                'origen':      'whatsapp-nlp'
            }
            db.collection('tareas').add(data)
            score = get_priority_score(data)
            tiempo_txt = {'rapida':'< 15min','corta':'15-45min','media':'1-2h','larga':'> 2h'}.get(data['tiempo'],'')
            fecha_txt = fmt_date(data['fecha']) if data['fecha'] else 'Sin fecha'
            imp_stars = '⭐' * data['importancia']
            urg_dots = '🔴' * data['urgencia']
            reply = (f"✅ *Tarea agendada*\n\n"
                    f"📌 *{data['titulo']}*\n"
                    f"Importancia: {imp_stars}\n"
                    f"Urgencia: {urg_dots}\n"
                    f"Tiempo: {tiempo_txt}\n"
                    f"Fecha: {fecha_txt}\n"
                    f"Categoría: {data['categoria']}\n"
                    f"Prioridad: ⚡{score} pts")
        elif interpretacion['accion'] == 'consultar':
            tipo = interpretacion.get('tipo', 'hoy')
            if tipo == 'hoy': reply = cmd_hoy(tasks)
            elif tipo == 'pendientes': reply = cmd_pendientes(tasks)
            elif tipo == 'vencidas': reply = cmd_vencidas(tasks)
            elif tipo == 'quickwin': reply = cmd_quickwins(tasks)
            else: reply = cmd_hoy(tasks)
        elif interpretacion['accion'] == 'completar':
            num = interpretacion.get('numero', 0)
            reply = cmd_completar(f'completar {num}', tasks)
        else:
            reply = cmd_ayuda()
    
    resp = MessagingResponse()
    resp.message(reply)
    return Response(str(resp), mimetype='application/xml')
 
@app.route('/health', methods=['GET'])
def health():
    return {'status': 'ok', 'bot': 'Gestor Tareas Edinson'}, 200
 
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
