import os
import re
import json
import yaml
import smtplib
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from jobspy import scrape_jobs

HISTORIAL_FILE = "historial_ofertas.json"

def cargar_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def cargar_historial():
    if os.path.exists(HISTORIAL_FILE):
        with open(HISTORIAL_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def guardar_historial(historial):
    with open(HISTORIAL_FILE, "w", encoding="utf-8") as f:
        json.dump(list(historial), f, indent=2)

def evaluar_oferta(titulo, descripcion, config):
    titulo_lower = str(titulo).lower()
    desc_lower = str(descripcion).lower()
    texto_completo = f"{titulo_lower} {desc_lower}"

    # 1. Descartar cargos Senior/Jefaturas o técnicos operativos en el título
    for excl in config["excluir_en_titulo"]:
        if excl.lower() in titulo_lower:
            return False, 0, []

    # 2. Descartar prácticas profesionales o memorias si está activado
    if config.get("excluir_practicas", True):
        palabras_practica = ["práctica profesional", "practica profesional", "alumno en práctica", "alumno en practica", "memorista", "tesis"]
        if any(p in titulo_lower for p in palabras_practica):
            return False, 0, []

    # 3. Descartar ofertas que exijan 2 o más años de experiencia
    max_anos = config.get("max_anos_experiencia", 1)
    patrones_experiencia = [
        r'(?:mínimo|minimo|al menos|más de|mas de|desde|sobre|entre)?\s*(\d+)\s*(?:\+|a\s*\d+|o\s*más|o\s*mas)?\s*años?\s+de\s+experiencia',
        r'experiencia\s+(?:mínima|minima|comprobable|previa|laboral)?\s*(?:de|mayor a|superior a)?\s*(\d+)\s*(?:\+|a\s*\d+|o\s*más|o\s*mas)?\s*años?'
    ]
    for patron in patrones_experiencia:
        for match in re.findall(patron, texto_completo):
            if int(match) > max_anos:
                return False, 0, []

    # 4. Verificar que pertenezca al área eléctrica / energía
    es_del_area = any(kw.lower() in texto_completo for kw in config["palabras_clave_area"])
    if not es_del_area:
        return False, 0, []

    # 5. Calcular puntaje de afinidad con tu CV
    puntaje = 10  # Puntaje base por ser del área
    coincidencias = []
    for kw, pts in config.get("puntajes_perfil", {}).items():
        if kw.lower() in texto_completo:
            puntaje += pts
            coincidencias.append(kw)

    return True, min(puntaje, 100), coincidencias

def buscar_en_portales(config, historial):
    ofertas_nuevas = []
    for termino in config["busquedas"]:
        try:
            jobs = scrape_jobs(
                site_name=config["portales"],
                search_term=termino,
                location=config["ubicacion"],
                results_wanted=15,
                hours_old=config["horas_antiguedad"],
                country_indeed="Chile"
            )
            if jobs is None or jobs.empty:
                continue

            for _, row in jobs.iterrows():
                url = str(row.get("job_url", ""))
                titulo = str(row.get("title", "Sin título"))
                empresa = str(row.get("company", "Empresa confidencial"))
                ubicacion = str(row.get("location", "Chile"))
                portal = str(row.get("site", ""))
                descripcion = str(row.get("description", ""))

                if not url or url in historial:
                    continue

                valida, puntaje, matches = evaluar_oferta(titulo, descripcion, config)
                if valida:
                    historial.add(url)
                    ofertas_nuevas.append({
                        "titulo": titulo,
                        "empresa": empresa,
                        "ubicacion": ubicacion,
                        "fuente": portal.capitalize(),
                        "url": url,
                        "puntaje": puntaje,
                        "matches": matches[:5]
                    })
        except Exception as e:
            print(f"Error buscando '{termino}': {e}")
    return ofertas_nuevas

def buscar_en_empresas_directas(config, historial):
    ofertas_directas = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for emp in config.get("empresas_directas", []):
        try:
            resp = requests.get(emp["url"], headers=headers, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                texto = a.get_text(strip=True)
                href = a["href"]
                if not href.startswith("http"):
                    href = requests.compat.urljoin(emp["url"], href)

                if len(texto) < 8 or href in historial:
                    continue

                valida, puntaje, matches = evaluar_oferta(texto, texto, config)
                if valida:
                    historial.add(href)
                    ofertas_directas.append({
                        "titulo": texto,
                        "empresa": emp["nombre"],
                        "ubicacion": "Chile (Web Directa)",
                        "fuente": "Portal Empresa",
                        "url": href,
                        "puntaje": puntaje + 15,  # Bonus por ser página directa de empresa clave
                        "matches": matches[:5]
                    })
        except Exception as e:
            print(f"No se pudo revisar {emp['nombre']}: {e}")
    return ofertas_directas

def enviar_correo(ofertas):
    remitente = os.environ["EMAIL_USER"]
    password = os.environ["EMAIL_PASS"]
    destinatario = os.environ.get("EMAIL_TO", remitente)

    if not ofertas:
        print("No hay ofertas nuevas hoy.")
        return

    # Ordenar de mayor a menor compatibilidad con tu CV
    ofertas.sort(key=lambda x: x["puntaje"], reverse=True)

    html = f"<h2>⚡ {len(ofertas)} Oportunidades Filtradas para tu Perfil</h2>"
    html += "<p style='color:#555;'>Ordenadas por afinidad con tu experiencia en estudios sistémicos, protecciones, electrónica de potencia y evaluación de proyectos:</p><ul>"

    for o in ofertas:
        if o["puntaje"] >= 55:
            etiqueta = f"🔥 Afinidad Alta ({o['puntaje']}%)"
            color = "#d9534f"
        elif o["puntaje"] >= 30:
            etiqueta = f"✅ Afinidad Media ({o['puntaje']}%)"
            color = "#2e7d32"
        else:
            etiqueta = f"📌 Exploratoria ({o['puntaje']}%)"
            color = "#666666"

        tags = ", ".join(o["matches"]) if o["matches"] else "Área eléctrica"

        html += (
            f"<li style='margin-bottom:16px; border-bottom:1px solid #eee; padding-bottom:10px;'>"
            f"<span style='color:{color}; font-weight:bold; font-size:13px;'>{etiqueta}</span><br>"
            f"<strong style='font-size:16px;'><a href='{o['url']}'>{o['titulo']}</a></strong><br>"
            f"🏢 {o['empresa']} | 📍 {o['ubicacion']} | 🌐 <em>{o['fuente']}</em><br>"
            f"<span style='font-size:12px; color:#444;'>🔑 Coincidencias: {tags}</span>"
            f"</li>"
        )
    html += "</ul>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚡ {len(ofertas)} ofertas compatibles con tu CV (Top: {ofertas[0]['puntaje']}%)"
    msg["From"] = remitente
    msg["To"] = destinatario
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(remitente, password)
        server.sendmail(remitente, destinatario, msg.as_string())
    print(f"Correo enviado con {len(ofertas)} ofertas.")

if __name__ == "__main__":
    config = cargar_config()
    historial = cargar_historial()

    nuevas_portales = buscar_en_portales(config, historial)
    nuevas_empresas = buscar_en_empresas_directas(config, historial)

    total = nuevas_portales + nuevas_empresas
    guardar_historial(historial)
    enviar_correo(total)
