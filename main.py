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
pending_tasks = {}  # guarda tareas esperando confirmación de fecha
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
 
def get_recurrentes():
    docs = db.collection('tareas_recurrentes').stream()
    return [{'id': d.id, **d.to_dict()} for d in docs]
 
def calcular_proxima_fecha(recurrencia, referencia=None):
    """Calcula la próxima fecha según la recurrencia"""
    hoy = referencia or datetime.now(LIMA_TZ).date()
    tipo = recurrencia.get('tipo')
    
    if tipo == 'diaria':
        return (hoy + timedelta(days=1)).isoformat()
    
    elif tipo == 'semanal':
        dia = recurrencia.get('dia_semana', 0)  # 0=lunes
        dias = (dia - hoy.weekday()) % 7 or 7
        return (hoy + timedelta(days=dias)).isoformat()
    
    elif tipo == 'mensual':
        # X días hábiles antes del cierre del mes
        dias_habiles_antes = recurrencia.get('dias_habiles_antes', 2)
        # Calcular último día del mes siguiente
        if hoy.month == 12:
            primer_sig = hoy.replace(year=hoy.year+1, month=1, day=1)
        else:
            primer_sig = hoy.replace(month=hoy.month+1, day=1)
        ultimo_mes = primer_sig - timedelta(days=1)
        # Restar días hábiles
        fecha = ultimo_mes
        habiles = 0
        while habiles < dias_habiles_antes:
            fecha -= timedelta(days=1)
            if fecha.weekday() < 5:
                habiles += 1
        return fecha.isoformat()
    
    elif tipo == 'quincenal':
        return (hoy + timedelta(days=15)).isoformat()
    
    return None
 
def procesar_recurrentes():
    """Genera tareas desde las recurrentes si corresponde"""
    recurrentes = get_recurrentes()
    hoy = datetime.now(LIMA_TZ).date().isoformat()
    
    for r in recurrentes:
        proxima = r.get('proxima_fecha')
        if proxima and proxima <= hoy:
            # Crear la tarea
            data = {
                'titulo':      r.get('titulo'),
                'descripcion': r.get('descripcion', f"Tarea recurrente: {r.get('tipo_label','')}"),
                'importancia': r.get('importancia', 4),
                'urgencia':    r.get('urgencia', 4),
                'dificultad':  r.get('dificultad', 2),
                'tiempo':      r.get('tiempo', 'corta'),
                'categoria':   r.get('categoria', 'trabajo'),
                'fecha':       proxima,
                'estado':      'pendiente',
                'creadoEl':    hoy,
                'completadoEl': '',
                'origen':      'recurrente'
            }
            db.collection('tareas').add(data)
            
            # Actualizar próxima fecha
            nueva_proxima = calcular_proxima_fecha(r, datetime.now(LIMA_TZ).date())
            db.collection('tareas_recurrentes').document(r['id']).update({
                'proxima_fecha': nueva_proxima,
                'ultima_generada': hoy
            })
            print(f"Tarea recurrente generada: {r.get('titulo')} -> proxima: {nueva_proxima}")
 
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
 
# ── MENSAJES MOTIVADORES CON CLAUDE ─────────────────────
def generar_motivacion(tipo):
    hoy = datetime.now(LIMA_TZ)
    tasks = get_tasks()
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente']
    completadas_hoy = [t for t in tasks if t.get('estado') == 'completada'
                       and t.get('completadoEl') == hoy.date().isoformat()]
    vencidas = [t for t in pendientes if is_overdue(t.get('fecha',''))]
    hoy_tasks = [t for t in pendientes if t.get('fecha') == hoy.date().isoformat()]
 
    contextos = {
        'afirmacion': (
            f"Genera una afirmacion poderosa de exito, abundancia y prosperidad para Edinson Pastor, "
            f"profesional SSOMA en Lima Peru. Hoy es {hoy.strftime('%A %d de %B')}. "
            f"El mensaje debe ser personal, energizante y orientado a mentalidad de exito financiero "
            f"y profesional. Maximo 4 lineas. Usa un tono inspirador pero realista. "
            f"Incluye una afirmacion en primera persona. Sin emojis excesivos, maximo 2."
        ),
        'arranque': (
            f"Genera un mensaje motivador de arranque matutino para Edinson Pastor, profesional SSOMA. "
            f"Tiene {len(hoy_tasks)} tareas hoy, {len(vencidas)} vencidas, {len(pendientes)} pendientes total. "
            f"El mensaje debe empujarlo a ser productivo, evitar procrastinar y enfocarse en cerrar tareas. "
            f"Hazlo personal y directo. Maximo 3 lineas. Menciona algo concreto de su dia si aplica."
        ),
        'mediodia': (
            f"Genera un mensaje motivador de medio dia para Edinson. "
            f"Completo {len(completadas_hoy)} tareas esta manana. Tiene {len(hoy_tasks)} pendientes para hoy. "
            f"{'Tiene '+str(len(vencidas))+' tareas vencidas.' if vencidas else 'Sin tareas vencidas.'} "
            f"El mensaje debe hacer un balance honesto de la manana y motivarlo para la tarde. "
            f"Directo, sin palabreria. Maximo 3 lineas."
        ),
        'cierre': (
            f"Genera un mensaje motivador de cierre de tarde para Edinson. "
            f"Son las 3 PM, quedan 2h45 para la salida (5:45 PM). "
            f"Completo {len(completadas_hoy)} tareas hoy. Tiene {len(hoy_tasks)} pendientes. "
            f"El mensaje debe crear urgencia positiva para cerrar pendientes antes de salir. "
            f"Que sienta que cada tarea cerrada es un logro profesional. Maximo 3 lineas."
        )
    }
 
    prompt = (
        f"Eres el asistente personal de Edinson Pastor. {contextos[tipo]} "
        f"Responde SOLO el mensaje, sin explicaciones, sin JSON, en espanol natural."
    )
 
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
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=12.0
        )
        result = response.json()
        return result['content'][0]['text'].strip()
    except Exception as e:
        print(f"Error motivacion: {e}")
        return None
 
def motivacion_afirmacion():
    msg = generar_motivacion('afirmacion')
    if msg:
        send_to_edinson(f"🌟 *Buenos dias, Edinson*\n\n{msg}")
 
def motivacion_arranque():
    msg = generar_motivacion('arranque')
    if msg:
        send_to_edinson(f"🚀 *A trabajar*\n\n{msg}")
 
def motivacion_mediodia():
    msg = generar_motivacion('mediodia')
    if msg:
        send_to_edinson(f"⚡ *Mitad del dia*\n\n{msg}")
 
def motivacion_cierre():
    msg = generar_motivacion('cierre')
    if msg:
        send_to_edinson(f"🎯 *Ultimas horas*\n\n{msg}")
 
# ── INICIAR SCHEDULER ─────────────────────────────────────
scheduler = BackgroundScheduler(timezone=LIMA_TZ)
# 7:00 AM Lima — Generar tareas recurrentes del día
scheduler.add_job(procesar_recurrentes, CronTrigger(hour=7, minute=0, timezone=LIMA_TZ))
# 7:30 AM Lima — Buenos días + resumen matutino
scheduler.add_job(reminder_manana,     CronTrigger(hour=7,  minute=30, timezone=LIMA_TZ))
# 9:00 PM Lima — Resumen nocturno
scheduler.add_job(reminder_nocturno,   CronTrigger(hour=21, minute=0,  timezone=LIMA_TZ))
# 2:00 PM Lima — Seguimiento de tarde
scheduler.add_job(reminder_seguimiento,CronTrigger(hour=14, minute=0,  timezone=LIMA_TZ))
# 7:35 AM Lima — Alertas de vencimiento 7 y 3 días hábiles
scheduler.add_job(reminder_vencimientos,  CronTrigger(hour=7,  minute=35, timezone=LIMA_TZ))
# Mensajes motivadores diarios
scheduler.add_job(motivacion_afirmacion,  CronTrigger(hour=8,  minute=0,  timezone=LIMA_TZ))
scheduler.add_job(motivacion_arranque,    CronTrigger(hour=9,  minute=0,  timezone=LIMA_TZ))
scheduler.add_job(motivacion_mediodia,    CronTrigger(hour=12, minute=30, timezone=LIMA_TZ))
scheduler.add_job(motivacion_cierre,      CronTrigger(hour=15, minute=0,  timezone=LIMA_TZ))
scheduler.start()
print("Scheduler iniciado — recordatorios activos para Lima (UTC-5)")
 
# ── PROCESADOR CON CLAUDE FLUIDO ─────────────────────────
def procesar_con_claude(mensaje, tasks):
    hoy = datetime.now(LIMA_TZ)
    pendientes = [t for t in tasks if t.get('estado') == 'pendiente']
    vencidas   = [t for t in pendientes if is_overdue(t.get('fecha',''))]
    top5 = sorted(pendientes, key=lambda x: -get_priority_score(x))[:5]
 
    resumen = ""
    for i, t in enumerate(top5, 1):
        over = is_overdue(t.get('fecha',''))
        dias = days_overdue(t.get('fecha','')) if over else 0
        resumen += f"  {i}. {t.get('titulo')} | {get_priority_label(t)} | {get_priority_score(t)}pts"
        if t.get('fecha'): resumen += f" | {fmt_date(t.get('fecha'))}"
        if over: resumen += f" | {dias}d retraso"
        resumen += "\n"
 
    manana = (hoy + timedelta(days=1)).strftime('%Y-%m-%d')
 
    prompt = (
        f"Eres un asistente personal de Edinson Pastor, profesional SSOMA en Electro Enchufe SAC Lima Peru.\n"
        f"Eres cercano, directo y profesional. Conoces bien el trabajo SSOMA: EPPs, SCTR, IPERC, inspecciones, actas, charlas, EMO.\n"
        f"Llamas siempre a tu usuario Edinson, nunca Eddy ni otro apodo. Eres conciso pero calido. Nunca eres robotico.\n\n"
        f"HOY: {hoy.strftime('%A %d de %B de %Y, %H:%M')} Lima Peru\n"
        f"Jornada: 7:30 AM - 5:45 PM | Almuerzo: 12:00-2:00 PM\n"
        f"Pendientes: {len(pendientes)} | Vencidas: {len(vencidas)}\n"
        f"Top tareas:\n{resumen}\n"
        f"MENSAJE: \"{mensaje}\"\n\n"
        f"Responde SOLO con JSON sin texto adicional.\n\n"
        f"Si quiere CREAR tarea:\n"
        f'{{"accion":"crear","titulo":"titulo","fecha":"YYYY-MM-DD o null",'
        f'"importancia":3,"urgencia":3,"dificultad":3,'
        f'"tiempo":"rapida/corta/media/larga","categoria":"trabajo/personal/estudio/salud",'
        f'"respuesta_fluida":"mensaje natural max 2 lineas confirmando"}}\n\n'
        f"Si quiere CONSULTAR:\n"
        f'{{"accion":"consultar","tipo":"hoy/pendientes/vencidas/quickwin"}}\n\n'
        f"Si quiere COMPLETAR:\n"
        f'{{"accion":"completar","numero":1}}\n\n'
        f"Si es saludo, charla o pregunta general:\n"
        f'{{"accion":"conversar","respuesta_fluida":"respuesta natural util max 3 lineas"}}\n\n'
        f"Fechas: hoy={hoy.strftime('%Y-%m-%d')}, manana={manana}\n"
        f"Importancia/urgencia: urgente=5, importante=4, normal=3, puede esperar=2, algun dia=1\n"
        f"Tiempo: <15min=rapida, 15-45min=corta, 1-2h=media, >2h=larga\n"
        f"SSOMA keywords -> categoria:trabajo, importancia>=4\n"
        f"Si menciona frecuencia (todos los meses, cada semana, cada lunes, etc) -> accion:recurrente\n"
        f"Para recurrente: {{\"accion\":\"recurrente\",\"titulo\":\"titulo\",\"tipo\":\"mensual/semanal/diaria/quincenal\",\"dia_semana\":0-6,\"dias_habiles_antes\":N,\"importancia\":1-5,\"urgencia\":1-5,\"dificultad\":1-5,\"tiempo\":\"rapida/corta/media/larga\",\"categoria\":\"trabajo\",\"respuesta_fluida\":\"confirmacion natural\"}}"
    )
 
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
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=12.0
        )
        result = response.json()
        text = result['content'][0]['text'].strip()
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
    
    # ── Respuesta de fecha pendiente ──────────────────────────
    if sender in pending_tasks:
        data = pending_tasks[sender]
        hoy = datetime.now(LIMA_TZ).date()
 
        fecha_resuelta = None
        inc_lower = incoming.strip().lower()
 
        # Normalizar y extraer día si viene en frase como "PUSE JUEVES", "es el viernes"
        inc_norm = inc_lower.strip()
        inc_norm = inc_norm.replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u')
        # Extraer solo el día si viene acompañado de otras palabras
        dias_semana = ['lunes','martes','miercoles','jueves','viernes','sabado','domingo']
        for dia in dias_semana:
            if dia in inc_norm and inc_norm != dia:
                inc_norm = dia  # quedarse solo con el día
                break
        # También extraer hoy/mañana si viene en frase
        if inc_norm != 'hoy' and 'hoy' in inc_norm and len(inc_norm) < 20:
            inc_norm = 'hoy'
        if inc_norm not in ['manana','mañana'] and ('manana' in inc_norm or 'mañana' in inc_norm) and len(inc_norm) < 25:
            inc_norm = 'manana'
 
        if inc_norm in ['hoy', 'today', 'hoy mismo']:
            fecha_resuelta = hoy.isoformat()
        elif inc_norm in ['mañana', 'manana', 'tomorrow', 'el dia de mañana']:
            fecha_resuelta = (hoy + timedelta(days=1)).isoformat()
        elif inc_norm in ['sin fecha', 'no tiene', 'sin', 'ninguna', 'no', 'sin fecha por ahora']:
            fecha_resuelta = ''
        elif inc_norm in ['lunes', 'el lunes', 'monday', 'el proximo lunes', 'proximo lunes']:
            d = hoy + timedelta(days=(0 - hoy.weekday()) % 7 or 7)
            fecha_resuelta = d.isoformat()
        elif inc_norm in ['martes', 'el martes', 'tuesday', 'el proximo martes', 'proximo martes']:
            d = hoy + timedelta(days=(1 - hoy.weekday()) % 7 or 7)
            fecha_resuelta = d.isoformat()
        elif inc_norm in ['miercoles', 'el miercoles', 'wednesday', 'proximo miercoles']:
            d = hoy + timedelta(days=(2 - hoy.weekday()) % 7 or 7)
            fecha_resuelta = d.isoformat()
        elif inc_norm in ['jueves', 'el jueves', 'thursday', 'proximo jueves', 'el proximo jueves']:
            d = hoy + timedelta(days=(3 - hoy.weekday()) % 7 or 7)
            fecha_resuelta = d.isoformat()
        elif inc_norm in ['viernes', 'el viernes', 'friday', 'proximo viernes', 'el proximo viernes']:
            d = hoy + timedelta(days=(4 - hoy.weekday()) % 7 or 7)
            fecha_resuelta = d.isoformat()
        elif inc_norm in ['sabado', 'el sabado', 'saturday']:
            d = hoy + timedelta(days=(5 - hoy.weekday()) % 7 or 7)
            fecha_resuelta = d.isoformat()
        elif inc_norm in ['domingo', 'el domingo', 'sunday']:
            d = hoy + timedelta(days=(6 - hoy.weekday()) % 7 or 7)
            fecha_resuelta = d.isoformat()
        else:
            # Intentar parsear DD/MM o DD de mes
            m = re.search(r'(\d{1,2})[/\-](\d{1,2})', inc_lower)
            if m:
                day, month = int(m.group(1)), int(m.group(2))
                year = hoy.year
                if month < hoy.month: year += 1
                try:
                    from datetime import date
                    fecha_resuelta = date(year, month, day).isoformat()
                except: pass
            else:
                meses = {'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,
                         'julio':7,'agosto':8,'septiembre':9,'octubre':10,'noviembre':11,'diciembre':12}
                for mes_nom, mes_num in meses.items():
                    mm = re.search(rf'(\d{{1,2}})\s+de\s+{mes_nom}', inc_lower)
                    if mm:
                        try:
                            from datetime import date
                            year = hoy.year
                            if mes_num < hoy.month: year += 1
                            fecha_resuelta = date(year, mes_num, int(mm.group(1))).isoformat()
                        except: pass
                        break
 
        if fecha_resuelta is not None:
            data['fecha'] = fecha_resuelta
            del pending_tasks[sender]
            db.collection('tareas').add(data)
            score = get_priority_score(data)
            tiempo_txt = {'rapida':'< 15min','corta':'15-45min','media':'1-2h','larga':'> 2h'}.get(data['tiempo'],'')
            imp_stars = '⭐' * data['importancia']
            urg_dots = '🔴' * data['urgencia']
            fecha_txt = fmt_date(data['fecha']) if data['fecha'] else 'Sin fecha'
            reply = (f"✅ *Tarea agendada*\n\n"
                    f"📌 *{data['titulo']}*\n"
                    f"Importancia: {imp_stars}\n"
                    f"Urgencia: {urg_dots}\n"
                    f"Tiempo: {tiempo_txt}\n"
                    f"Fecha: {fecha_txt}\n"
                    f"Categoría: {data['categoria']}\n"
                    f"Prioridad: ⚡{score} pts")
            resp = MessagingResponse()
            resp.message(reply)
            return Response(str(resp), mimetype='application/xml')
        else:
            reply = ("No entendí la fecha. Responde solo el día:\n"
                    "*jueves*, *viernes*, *lunes*\n"
                    "o una fecha: *15/04*\n"
                    "o escribe *sin fecha*")
            resp = MessagingResponse()
            resp.message(reply)
            return Response(str(resp), mimetype='application/xml')
 
    # Comandos directos rápidos (solo si el mensaje es corto y exacto)
    # Mensajes largos (más de 5 palabras) siempre van a Claude para no perder tareas
    palabras = incoming.strip().split()
    es_mensaje_largo = len(palabras) > 4
 
    if not es_mensaje_largo and incoming in ['hoy', 'resumen', 'qué hay hoy', 'que hay hoy']:
        reply = cmd_hoy(tasks)
    elif not es_mensaje_largo and incoming in ['pendientes', 'lista', 'tareas', 'mis tareas']:
        reply = cmd_pendientes(tasks)
    elif not es_mensaje_largo and incoming in ['quickwin', 'quick win', 'quickwins']:
        reply = cmd_quickwins(tasks)
    elif not es_mensaje_largo and incoming in ['vencidas', 'atrasadas']:
        reply = cmd_vencidas(tasks)
    elif not es_mensaje_largo and incoming in ['ayuda', 'help', 'comandos', 'menu']:
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
            respuesta_fluida = interpretacion.get('respuesta_fluida', '')
            if not data['fecha']:
                pending_tasks[sender] = data
                reply = (f"📌 *{data['titulo']}*\n\n"
                        f"Para que fecha la agendamos?\n\n"
                        f"• *hoy* / *manana*\n"
                        f"• *lunes*, *martes*, *viernes*...\n"
                        f"• Fecha exacta: *15/04*\n"
                        f"• *sin fecha* si no aplica")
            else:
                db.collection('tareas').add(data)
                score = get_priority_score(data)
                if respuesta_fluida:
                    fecha_txt = fmt_date(data['fecha']) if data['fecha'] else 'sin fecha'
                    reply = f"✅ {respuesta_fluida}\n_Prioridad: {score}pts | {fecha_txt}_"
                else:
                    tiempo_txt = {'rapida':'< 15min','corta':'15-45min','media':'1-2h','larga':'> 2h'}.get(data['tiempo'],'')
                    reply = (f"✅ Listo, agendado.\n\n"
                            f"📌 *{data['titulo']}*\n"
                            f"Fecha: {fmt_date(data['fecha'])} | Prioridad: {score}pts\n"
                            f"Tiempo: {tiempo_txt}")
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
        elif interpretacion.get('accion') == 'recurrente':
            tipo = interpretacion.get('tipo', 'mensual')
            titulo = interpretacion.get('titulo', 'Tarea recurrente')
            tipo_labels = {
                'mensual': 'cada mes',
                'semanal': 'cada semana',
                'diaria':  'cada día',
                'quincenal': 'cada 15 días'
            }
            rec_data = {
                'titulo':             titulo,
                'tipo':               tipo,
                'tipo_label':         tipo_labels.get(tipo, tipo),
                'dia_semana':         interpretacion.get('dia_semana', 0),
                'dias_habiles_antes': interpretacion.get('dias_habiles_antes', 2),
                'importancia':        interpretacion.get('importancia', 4),
                'urgencia':           interpretacion.get('urgencia', 4),
                'dificultad':         interpretacion.get('dificultad', 2),
                'tiempo':             interpretacion.get('tiempo', 'corta'),
                'categoria':          interpretacion.get('categoria', 'trabajo'),
                'activa':             True,
                'creadoEl':           datetime.now(LIMA_TZ).date().isoformat(),
                'proxima_fecha':      calcular_proxima_fecha(interpretacion)
            }
            db.collection('tareas_recurrentes').add(rec_data)
            fluida = interpretacion.get('respuesta_fluida', '')
            proxima = fmt_date(rec_data['proxima_fecha']) if rec_data['proxima_fecha'] else 'próximamente'
            if fluida:
                reply = f"🔄 {fluida}\n_Primera aparición: {proxima}_"
            else:
                reply = (f"🔄 *Tarea recurrente creada*\n\n"
                        f"📌 *{titulo}*\n"
                        f"Frecuencia: {tipo_labels.get(tipo, tipo)}\n"
                        f"Primera aparición: {proxima}\n"
                        f"Se creará automáticamente cada vez que corresponda")
        elif interpretacion.get('accion') == 'conversar':
            reply = interpretacion.get('respuesta_fluida', '¿En que te puedo ayudar?')
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
