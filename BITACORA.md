# 📓 BITÁCORA — NAS Organizer

> Documento de continuidad del proyecto. Léelo completo antes de retomar el trabajo
> (con cualquier IA o desarrollador). Mantenlo actualizado al final de cada sesión.
>
> **Última actualización:** 2026-06-08

---

## 1. Qué es y por qué existe

**Problema original del usuario:** descarga vídeos con **MyJDownloader** (Docker en su NAS
Synology) a una carpeta con nombres arbitrarios y sin metadatos. Luego, manualmente, tiene
que mover cada archivo a las carpetas ordenadas que lee **Jellyfin** (Películas, Series,
Música), renombrarlo y buscarle metadatos. Tedioso y propenso a errores.

**Solución (este proyecto):** una app web propia, **semi-automática**, que vigila la carpeta
de descargas, identifica cada archivo (película / serie / música), propone metadatos + póster,
y deja que el usuario, con **un clic**, lo renombre y lo mueva a la carpeta correcta de
Jellyfin (eligiendo él la carpeta), refrescando Jellyfin después.

**Filosofía:** sencillez para un usuario **no técnico** y latino. UI en español. El usuario
decide; la app sugiere y ejecuta.

---

## 2. Estado actual (qué funciona) ✅

- Vigila la carpeta de descargas (sondeo cada 30 s + botón "Buscar ahora").
- Identifica tipo y datos con **guessit** (nombre) + **TheMovieDB** (vídeo) + **mutagen /
  MusicBrainz** (música).
- **Bandeja de revisión** con pestañas: Películas · Series · Música · Historial · Ajustes.
- Por cada descarga: póster + título + descripción (en **es-MX**), y botones
  Confirmar / Editar-buscar / Cambiar tipo / Omitir / Eliminar.
- **Navegador de carpetas tipo árbol** para elegir el destino (entrar/salir, crear subcarpeta)
  con **vista previa** de la ruta final.
- **Movimiento en segundo plano** (no congela la web) + **instantáneo** (montaje único de
  `/volume1`, sin copiar) + **protección anti-sobrescritura** (`… (2)`).
- Arrastra **subtítulos** junto al vídeo.
- **Refresco de Jellyfin** incremental tras mover + botón "Actualizar todo".
- **PWA instalable** (icono propio, pantalla completa).
- **SQLite en modo WAL** (la web va fluida aunque el vigilante escriba).
- Re-búsqueda automática de metadatos para pendientes al poner la API key.
- **Despliegue:** imagen Docker construida sola en **GitHub Actions → GHCR (pública)**,
  corriendo en **Synology Container Manager**. Actualización con `pull_policy: always`.

---

## 3. Arquitectura y archivos clave

- **Backend:** FastAPI (`app/main.py`, 250 líneas) — rutas web + API.
- **Frontend:** Jinja2 + **JS vanilla** (sin build, sin CDN). Plantillas en `app/templates/`.
- **BD:** SQLite con `sqlite3` directo (`app/db.py`), modo WAL. Tabla `items` + `settings`.
- **Config:** `app/config.py` — env vars como defaults, la BD (pestaña Ajustes) tiene prioridad.

| Archivo | Responsabilidad |
|---|---|
| `app/main.py` | Rutas FastAPI (tabs, confirm, scan, settings, api/folders, sw.js). Movimiento en hilo de fondo. |
| `app/watcher.py` | Vigila descargas, identifica y enriquece. `scan_once`, `reenrich_pending`, `_loop`. |
| `app/identify.py` | guessit → tipo (movie/series/music) + título/año/temporada/episodio. |
| `app/metadata/tmdb.py` | Búsqueda en TheMovieDB (v3 api_key). `search`, `best_match`. |
| `app/metadata/music.py` | Etiquetas con mutagen + respaldo MusicBrainz. |
| `app/organizer.py` | `build_dest`/`leaf_path` (estructura Jellyfin) + `move_item` + `unique_path`. |
| `app/folders.py` | Navegador de carpetas seguro dentro de las raíces. `browse`, `ensure_folder`, `within_roots`. |
| `app/jellyfin.py` | Refresco incremental / completo vía API. |
| `app/db.py` | Acceso SQLite (WAL), migraciones por `ALTER TABLE`, `reset_processing`. |

**Estructura destino (convención Jellyfin):**
- Películas: `…/Título (Año)/Título (Año).ext`
- Series: `…/Título/Season 01/Título S01E02.ext`
- Música: `…/Artista/Álbum/01 - Canción.ext`

**Esquema BD `items` (campos relevantes):** `original_path` (UNIQUE), `filename`, `media_type`
(movie|series|music|unknown), `status` (pending|processing|done|skipped|error),
`detected_title/year`, `season`, `episode`, `tmdb_id`, `chosen_title/year`, `poster_url`,
`overview`, `artist`, `album`, `track_no`, `dest_folder`, `dest_path`, `error`. Migraciones:
columnas añadidas con `ALTER TABLE` desde `_MIGRATIONS` en `db.py`.

---

## 4. Entorno real del usuario (para retomar)

- **NAS:** Synology, volumen `/volume1`. Repo desplegado en `/volume1/docker/nas-organizer/`.
- **Descargas (JDownloader):** `/volume1/homes/rnd261190/jdownloader`
- **Biblioteca vídeo:** `/volume1/video` (subcarpetas `peliculas`, `series`, y más que añade).
- **Biblioteca música:** `/volume1/music`
- **Puerto de la app:** `8678`. **Jellyfin:** `8096`.
- **Acceso:** IP local (misma red) o **Tailscale** (fuera de casa; la 100.x es del NAS, NO del PC).
  También DSM por QuickConnect (`ronyrnd`).
- **Idioma metadatos:** `es-MX`.
- **Repo GitHub:** `RonMarty2/NAS`, rama `claude/exciting-einstein-BexEa` (**público**).
- **Imagen:** `ghcr.io/ronmarty2/nas-organizer:latest` (**paquete público**).
- **Secretos (NO versionados):** API key TMDB y API key/URL Jellyfin → se ponen en **Ajustes**
  (se guardan en la BD `/data/nas.db`). El repo es público: **nunca** commitear claves.

---

## 5. Despliegue y actualización (Synology) — TRAMPAS APRENDIDAS

- El proyecto usa **imagen** (no build). Para crearlo: Container Manager → Proyecto → Crear →
  apuntar a la carpeta con `docker-compose.yml` → pega el compose si hace falta → Listo.
- **Carpeta `data/`** debe existir junto al compose (el bind mount `./data:/data` falla si no).
  Por eso se versiona `data/.gitkeep`.
- **Actualizar a la última versión** (proyecto por imagen):
  1. Acción → **Detener** (solo apaga; el contenedor SIGUE existiendo).
  2. Acción → **Limpiar** (esto SÍ borra el contenedor; necesario para liberar la imagen).
  3. Menú **Imagen** → borrar `ghcr.io/ronmarty2/nas-organizer` (ya "Sin usar").
  4. Acción → **Construir** (¡no "Iniciar"! "Iniciar" falla con *no container found* tras Limpiar).
  - Con `pull_policy: always` (ya añadido), en teoría basta **Detener → Construir** y baja la nueva.
- **Gotchas Synology:**
  - No existe botón "Reconstruir" en proyectos por imagen.
  - "Detener" ≠ quitar contenedor; usar "Limpiar" para liberar la imagen.
  - "Iniciar" solo enciende un contenedor existente; "Construir" lo crea (= `compose up`).
  - No hay paquete "Text Editor" por defecto (instalar desde Centro de paquetes para editar).
  - Acceso por puntos, no guiones: `192.168.100.178:8678` (no `192-168-...`).

---

## 6. Decisiones tomadas (y por qué)

- **App propia** en vez de FileBot/*arr: el usuario quería UI simple "revisar y aprobar";
  FileBot le resultó confuso; *arr no se integra con JDownloader.
- **El usuario elige la carpeta** (árbol) en vez de auto-decidir: tiene muchas carpetas y
  añade más; quería control.
- **Montar `/volume1` entero**: hace los movimientos instantáneos (mismo dispositivo = rename)
  y las rutas coinciden con las del NAS (menos confusión).
- **Imagen en GHCR + Actions**: actualización sin re-descargar ZIP ni SSH (repo privado→ZIP era
  tedioso; se hizo público para simplificar).
- **WAL + hilos de fondo**: la app se colgaba/iba lenta con movimientos grandes y escrituras.
- **PWA sin service worker en http**: se registra solo en contexto seguro; instalación por
  "Añadir a pantalla de inicio".

---

## 7. Problemas conocidos / limitaciones ⚠️

1. **Sin autenticación** — cualquiera en la red/Tailscale puede mover/borrar archivos.
2. **Solo HTTP** — sin cifrado; instalación PWA completa limitada en Android.
3. **Basura y "extras" se cuelan** como películas (samples, activadores, `fVUdw.mp4`,
   `T01 - Extra …`). Solo se filtra por tamaño mínimo (10 MB).
4. **Nombres ofuscados** (leetspeak: `Str1pt3as3`) a veces no hacen match en TMDB → edición manual.
5. **Música** es "mejor esfuerzo": no escribe etiquetas/portada, matching MusicBrainz débil.
6. **No escribe `.nfo`** con tmdbid → Jellyfin re-identifica por nombre de carpeta (puede
   elegir mal, o mostrar el título en el idioma de Jellyfin, no el de la app).
7. **Paquetes/carpetas** (temporadas completas): cada archivo es un item separado; no se agrupan.
8. **Jellyfin muestra en SU idioma**, no en el de la app; hay que configurarlo en Jellyfin.
9. **Sin notificaciones**: hay que abrir la app para ver lo nuevo.
10. **Sin tests** (CI solo construye la imagen).
11. **Sin "deshacer"** un movimiento.
12. **Sin detección de duplicados** ya presentes en la biblioteca.
13. **Claves en texto plano** en la BD (riesgo bajo, uso personal).

---

## 8. ROADMAP de mejoras (priorizado)

### 🔴 Alta prioridad (impacto directo en el uso diario)
- ✅ **A. Filtro de basura (HECHO):** `junk_patterns` (config) + `watcher._is_junk`. Ignora
  sample/activador/crack/keygen/trailer/etc. Editable en Ajustes.
- ✅ **B. Leetspeak (HECHO):** `tmdb._deleet` reintenta `Str1pt3as3` → `Striptease` como respaldo.
- ✅ **C. Notificaciones (HECHO):** `app/notify.py` (ntfy / Discord / Telegram). Avisa al llegar
  descargas nuevas + botón de prueba en Ajustes. Campos en Ajustes.
- ✅ **D. `.nfo` + póster local (HECHO):** `organizer.write_metadata` escribe `movie.nfo` /
  `tvshow.nfo` con `tmdbid` y guarda `poster.jpg`. Jellyfin matchea exacto y respeta el título.
- **E. Autenticación simple** (contraseña/PIN) — seguridad si se accede por Tailscale/remoto.
  *(Pendiente — siguiente candidato de alta prioridad.)*

### 🟡 Media prioridad
- **F. Acciones en lote:** "Confirmar todos los reconocidos", "Omitir/Eliminar no reconocidos".
- **G. Agrupar por paquete/carpeta** (temporadas completas) y confirmar en bloque.
- **H. HTTPS con Tailscale Serve** — cifrado + PWA nativa en Android.
- **I. Mejorar Música:** portada, escribir etiquetas, carpeta por género, mejor matching.
- **J. Detección de duplicados** ya en la biblioteca (avisar antes de mover).
- **K. Botón "Deshacer" último movimiento.**
- **L. Idioma con fallback** (es-MX → es → en) cuando no hay match en latino.

### 🟢 Baja prioridad / futuro
- **M. Tests automatizados** + correrlos en CI.
- **N. Limpieza de carpetas vacías** en descargas tras mover; borrar basura del paquete.
- **O. Estadísticas** (movidas, espacio liberado, no reconocidos).
- **P. Opción "película directa sin subcarpeta"** (configurable).
- **Q. Integración opcional con Sonarr/Radarr** o Trakt.
- **R. Reintentar items con error** con un botón; ver errores claramente.

---

## 9. Cómo retomar (desarrollo local)

```bash
pip install -r requirements.txt
export NAS_DB_PATH=/tmp/nas.db NAS_DOWNLOADS_DIR=/ruta/descargas \
       NAS_LIBRARY_ROOTS=/ruta/video,/ruta/music
uvicorn app.main:app --reload --port 8678
# Abrir http://localhost:8678
```

- **Probar lógica** sin red: crear archivos de prueba en la carpeta de descargas y llamar a
  `watcher.scan_once()`; ver `app/organizer.py` para rutas destino.
- **CI/imagen:** cualquier push a la rama dispara `.github/workflows/docker.yml` →
  reconstruye `ghcr.io/ronmarty2/nas-organizer:latest`.
- **Convención de trabajo:** ramas de desarrollo según indique el usuario; commits descriptivos
  en español; no commitear secretos (repo público).

---

## 10. Registro de sesiones

### Sesión 1 — 2026-06-08 (creación completa)
Construido de cero hasta producción funcionando en el NAS del usuario:
app base → música → selector de carpeta → imagen automática (GHCR) → PWA → arreglos de
fluidez (WAL, fondo) → es-MX → protección anti-sobrescritura. Desplegado y verificado:
mueve "Striptease (1996)" y "Alerta Extinción (2026)" correctamente con póster en latino.
Pendiente: conectar Jellyfin (URL+API key), y elegir mejoras del roadmap (§8).

### Sesión 2 — 2026-06-08 (mejoras del roadmap A–D)
- Implementadas y verificadas: **A** filtro de basura (`junk_patterns`/`_is_junk`),
  **B** leetspeak (`tmdb._deleet`), **C** notificaciones (`app/notify.py`: ntfy/Discord/Telegram
  + botón de prueba), **D** `.nfo` + póster local (`organizer.write_metadata`).
- Nota operativa: el repo **local** se había revertido a `cdd01cb`; se recuperó todo con
  `git reset --hard origin/<rama>` (el remoto tenía todo). **Siempre** verificar
  `git log` vs `origin/` al iniciar sesión por si el contenedor se re-clonó atrás.
- Config nueva: `junk_patterns`, `app_url`, `ntfy_server/topic`, `discord_webhook`,
  `telegram_token/chat_id` (todo editable en Ajustes).

> **Próximo paso sugerido:** **E. Autenticación** (contraseña) por seguridad, y/o **F.
> acciones en lote** (confirmar todos los reconocidos / borrar no reconocidos). Recordar al
> usuario: para usar las funciones nuevas debe **actualizar** el contenedor (Detener →
> Construir, gracias a `pull_policy: always`) y poner el `app_url` + un canal de notificación.
