# 📓 BITÁCORA — NAS Organizer

> Documento de continuidad del proyecto. Léelo completo antes de retomar el trabajo
> (con cualquier IA o desarrollador). Mantenlo actualizado al final de cada sesión.
>
> **Última actualización:** 2026-06-09
>
> ⚠️ **RAMA ACTIVA: `main`** (no `claude/exciting-einstein-BexEa`). Ver §4 y §11.

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
- Re-búsqueda automática de metadatos para pendientes al poner la API key
  (con tope de **3 intentos** por item para no machacar TMDB; se reinicia al guardar Ajustes).
- **Series agrupadas**: una tarjeta por serie (no una por episodio), expandible, con
  **mover en bloque** ("Confirmar y mover los N") y verificación nombre→nombre final.
- **Detección de duplicados** entre pendientes (mismo destino + tamaño + **SHA-256**):
  comparación lado a lado, borrado individual y **en lote** (por serie y **global** en Ajustes),
  conservando siempre una copia legible. Verifica SHA antes de borrar (no corrompe).
- **Datos del archivo** (peso, calidad/resolución, códec, idioma, duración) desde el nombre
  y, si se activa, **ffprobe** (`app/filemeta.py`). Avisos si el destino ya existe (`targets.py`).
- **Notificaciones** al llegar descargas (ntfy / Discord / Telegram) — `app/notify.py`.
- **`.nfo` + póster local** al mover (`organizer.write_metadata`): Jellyfin matchea exacto.
- **Móvil**: responsivo (episodios en bloques, botones grandes), **contadores por pestaña**,
  y la auto-recarga conserva scroll + episodios abiertos (no "salta").
- **Despliegue:** imagen Docker construida sola en **GitHub Actions → GHCR (pública)**,
  corriendo en **Synology Container Manager**. La imagen trae **ffmpeg** (ffprobe).

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
| `app/db.py` | Acceso SQLite (WAL), migraciones por `ALTER TABLE`, `reset_processing`, `pending_counts`, `reset_match_attempts`. |
| `app/duplicates.py` | Duplicados: `analyze`, `comparison_groups`, `delete_exact_duplicate` (1), `delete_all_exact_duplicates` (lote). SHA-256 + verificación de superviviente. |
| `app/filemeta.py` | Datos del archivo (nombre + ffprobe opcional): calidad, idioma, códec, duración, `media_is_readable`. |
| `app/targets.py` | Comprueba si el destino ya existe / tiene medios (avisos antes de mover). |
| `app/notify.py` | Notificaciones ntfy / Discord / Telegram (mejor esfuerzo). |
| `app/templates/series.html` | Vista de Series agrupada + comparación de duplicados + dedup en lote. |

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
- **Repo GitHub:** `RonMarty2/NAS` (**público**). **RAMA ACTIVA = `main`** (es la que el
  usuario tiene desplegada). La rama `claude/exciting-einstein-BexEa` quedó **obsoleta** (ver §11).
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
  - ⚠️ **`pull_policy` NO sirve en Synology** (Container Manager lo rechaza: "Additional
    property is not allowed"). Se quitó del compose. La actualización es **siempre** el
    método de 4 pasos de arriba (Detener → Limpiar → borrar imagen → Construir).
- **Gotchas Synology:**
  - No existe botón "Reconstruir" en proyectos por imagen.
  - "Detener" ≠ quitar contenedor; usar "Limpiar" para liberar la imagen.
  - "Iniciar" solo enciende un contenedor existente; "Construir" lo crea (= `compose up`).
  - No hay paquete "Text Editor" por defecto (instalar desde Centro de paquetes para editar).
  - Acceso por puntos, no guiones: `192.168.100.178:8678` (no `192-168-...`).
  - **IP Tailscale del NAS:** `100.112.204.87` (la app en `:8678`, el DSM en `:5000`).
- **⚠️ EL ENTORNO DE TRABAJO SE REVIERTE SOLO:** varias veces la copia LOCAL volvió a un commit
  viejo (re-clonado del contenedor). El **remoto siempre tuvo lo bueno**. **Al iniciar sesión,
  SIEMPRE:** `git fetch origin "+refs/heads/*:refs/remotes/origin/*"` y comparar `git log` local
  vs `origin/main`; si difiere, `git reset --hard origin/main`. No fíes del estado local.
- **PWA / "instalar app" en Android:** solo con **HTTPS**. Con `http://` Chrome NO instala
  (solo "Agregar a pantalla principal", que abre como página). "No abre" suele ser **Tailscale
  apagado** o entrar al `:5000` en vez del `:8678`. Solución nativa = **Tailscale Serve** (§8 H).

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

1. **Sin autenticación** — cualquiera en la red/Tailscale puede mover/borrar archivos. **(LO MÁS
   IMPORTANTE PENDIENTE.)**
2. **Solo HTTP** — sin cifrado; instalación PWA nativa limitada en Android (necesita Tailscale Serve).
3. **Extras se cuelan** (`T01 - Extra …`) y se marcan como "posible duplicado" del peli (el SHA
   evita borrarlos, pero confunde). El filtro de basura por nombre sí quita sample/crack/etc.
4. **Música** es "mejor esfuerzo": no escribe etiquetas/portada, matching MusicBrainz débil.
5. **Jellyfin muestra en SU idioma**, no en el de la app; hay que configurarlo en Jellyfin
   (mitigado al escribir `.nfo` con tmdbid, pero el idioma de la UI de Jellyfin manda).
6. **Sin tests** (CI solo construye la imagen).
7. **Sin "deshacer"** un movimiento. **Historial sin paginar** (crece sin límite).
8. **Sin detección de duplicados ya en la biblioteca** (solo entre pendientes). `targets.py` avisa
   si el destino existe, pero no compara contenido contra lo ya movido.
9. **Claves en texto plano** en la BD; **sin CSRF** (combinado con sin-login, una web podría
   disparar borrados). Riesgo bajo en uso personal.
10. **`ffprobe` desactivado por defecto** (`probe_media_info=false`): la calidad/idioma salen
    solo del nombre salvo que se active en Ajustes.

> **RESUELTO desde la última bitácora:** detección/borrado de duplicados (SHA-256, individual +
> lote + global), datos del archivo (peso/calidad/idioma/duración + ffprobe), `.nfo`+póster,
> notificaciones, agrupar series + mover en bloque, leetspeak, filtro de basura, vista móvil +
> contadores, y bugs (recarga infinita, cambiar-tipo re-identifica, throttle TMDB, scan vuelve a
> la pestaña).

---

## 8. ROADMAP de mejoras (priorizado)

**Ya HECHO:** A filtro de basura · B leetspeak · C notificaciones · D `.nfo`+póster ·
F/G series agrupadas + mover en bloque · J(parcial) duplicados entre pendientes (SHA-256,
individual+lote+global) · datos del archivo + ffprobe · vista móvil + contadores · varios bugs.

### 🔴 Alta prioridad (lo siguiente)
- **E. Autenticación simple** (contraseña/PIN) — **lo más importante pendiente** (acceso remoto
  por Tailscale sin login = cualquiera borra archivos).
- **H. HTTPS con Tailscale Serve** — cifrado + PWA nativa. Plan sin SSH: **Programador de tareas**
  del Synology corriendo `tailscale serve --bg 8678` (binario en `/var/packages/Tailscale/target/bin/`).
  Antes: activar MagicDNS + HTTPS Certificates en el admin de Tailscale.

### 🟡 Media prioridad
- **K. Deshacer último movimiento** + **paginar/limpiar el Historial** (crece sin fin).
- **Selección múltiple (casillas)** para confirmar/omitir un subconjunto.
- **Aviso de espacio en disco** antes de mover.
- **I. Mejorar Música:** portada, escribir etiquetas, mejor matching.
- **J2. Duplicados contra la biblioteca ya movida** (no solo pendientes).
- **L. Idioma con fallback** (es-MX → es → en) cuando no hay match en latino.
- **Optimizar:** cachear inspección de destino (hoy hace `os.listdir` por item en cada render);
  recarga parcial AJAX en vez de meta-refresh completo cada 6 s.

### 🟢 Baja prioridad / futuro
- **M. Tests automatizados** + CI. · **N. Limpiar carpetas vacías** tras mover.
- **O. Estadísticas** · **P. Película sin subcarpeta** · **Q. Sonarr/Radarr/Trakt** ·
  **R. Reintentar items con error**.

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

### Sesión 3 — 2026-06-09 (consolidación de ramas + auditoría)
- **Dos sesiones de Claude en paralelo** trabajaron el proyecto: una en `claude/exciting-einstein-BexEa`
  (esta bitácora, vista móvil, chips) y otra en **`main`** (duplicados SHA-256, `filemeta.py`,
  `targets.py`, comparación de duplicados). Ambas construían `:latest` → se pisaban. **El usuario
  decidió quedarse con `main`** (lo que ya usaba). Mis funciones de la otra rama estaban cubiertas
  por `main` a su manera, así que **no se perdió nada importante**. Ver §11.
- Sobre `main` se añadió: **borrado de duplicados en lote** (por serie) y **global** (Ajustes),
  ambos con verificación SHA-256 (`duplicates.delete_all_exact_duplicates`).
- **Auditoría** completa → se arreglaron bugs (recarga infinita tras dedup, "cambiar tipo"
  re-identifica, throttle de TMDB con `match_attempts`, "Buscar ahora" vuelve a la pestaña),
  se mejoró **móvil** (responsivo + contadores por pestaña + sin saltos al recargar), y se
  instaló **ffmpeg** con interruptor de "análisis profundo" en Ajustes.
- Commits en `main`: `…→ 68379bb → bac470c → 969ae27 → 36fd4e9 → e734042`.

> **Próximo paso sugerido:** **E. Autenticación** (contraseña/login), que el usuario aún no pidió
> pero es lo más importante de seguridad; luego HTTPS Tailscale, deshacer, paginar historial.
> Recordar al usuario: para ver lo nuevo debe **actualizar** el contenedor con el método de 4
> pasos de §5 (Detener → Limpiar → borrar imagen → Construir; `pull_policy` NO sirve en Synology).

---

## 11. Historia de las ramas (IMPORTANTE para no confundirse)

- El proyecto empezó en `claude/exciting-einstein-BexEa`. En cierto punto **otra sesión de Claude**
  empezó a trabajar en `main` y avanzó la parte de **duplicados y datos de archivo**.
- El workflow construye `:latest` desde `main` **y** desde `claude/**`, así que las dos ramas se
  pisaban la imagen desplegada (de ahí la sensación de "la app cambia sola de versión").
- **Decisión (sesión 3):** la rama **`main` es la oficial**. Ahí está todo: lo de la otra sesión
  (duplicados, `filemeta`, `targets`) **y** lo de esta (notificaciones, `.nfo`, series agrupadas,
  móvil, bugs, ffmpeg). `claude/exciting-einstein-BexEa` quedó **obsoleta** (no desarrollar ahí).
- **Punto de separación** de las ramas: commit `aa01b44`. Todo lo anterior es común.
- **Recomendación:** trabajar **solo en `main`** y mantener **una sola sesión** a la vez para no
  repetir el conflicto. Si hace falta, considerar borrar la rama vieja en GitHub.
### SesiÃ³n 4 â€” 2026-06-09 (mejor feedback al borrar duplicados)
- Se mejorÃ³ la UX de **borrado de duplicados** para que no parezca que "no pasa nada":
  el botÃ³n ahora se desactiva al confirmar, el estado de limpieza se muestra junto al bloque
  donde estÃ¡ el usuario y la pÃ¡gina se refresca sola mientras corre la verificaciÃ³n.
- La limpieza sigue siendo la misma de siempre: **SHA-256**, conserva una copia legible y
  no toca archivos que no sean idÃ©nticos.
- Se mantuvo la informaciÃ³n de estado unos segundos al terminar para que el usuario vea
  claramente que la acciÃ³n sÃ­ se ejecutÃ³.
### Sesión 5 — 2026-06-09 (progreso visible para borrado en masa)
- Se añadió **progreso real** al borrado en lote de duplicados: contador de archivos revisados,
  grupos procesados, archivos borrados y el archivo/grupo actual que se está calculando.
- La limpieza masiva ahora **recalcula SHA-256 de verdad** durante el proceso, para no depender
  de hashes viejos y mantener la verificación segura.
- Si algo falla, el banner ya no se queda mudo: muestra el **último error**, cuántos fallos hubo
  y cuántos grupos se omitieron por seguridad.

### Sesión 6 — 2026-06-09 (huérfanos y falsos duplicados)
- Se corrigió el caso en que la app seguía marcando como duplicado algo que ya no existía en disco.
- La detección de duplicados ahora **ignora archivos pendientes que ya no existen** y el escaneo
  limpia registros huérfanos antes de seguir.
- Resultado: si borras un archivo fuera de la app, en el siguiente escaneo ya no debería seguir
  apareciendo como duplicado.

### Sesión 7 — 2026-06-09 (tareas en segundo plano y continuidad)
- Se dejó documentado que las tareas largas de la app corren en el **servidor del NAS** (hilos en
  segundo plano), no dependen de que la pestaña siga abierta.
- Si el usuario cierra la página en PC o celular, el movimiento, el borrado masivo o la
  verificación de duplicados **siguen ejecutándose** mientras el contenedor siga vivo.
- Al reabrir la app, la interfaz vuelve a leer el estado guardado en la BD y muestra el progreso o
  el último resultado disponible.
- Si se reinicia o detiene el contenedor, esas tareas en curso sí se interrumpen y pueden quedar
  marcadas como pendientes para revisarlas de nuevo.

### Sesión 8 — 2026-06-09 (restaurar duplicados en la rama desplegada)
- El usuario reportó que ya no aparecían los duplicados lado a lado ni el botón para borrarlos
  de golpe. La causa fue que la copia local/GitHub seguía arrancando en
  `claude/exciting-einstein-BexEa`, una rama vieja sin la UI de duplicados.
- Se fusionó `origin/main` dentro de `claude/exciting-einstein-BexEa` para que, aunque Synology
  o GitHub usen la rama vieja por defecto, la imagen `latest` vuelva a incluir:
  comparación lado a lado, borrado individual verificado por SHA-256, borrado por serie y
  borrado global desde Ajustes.
- Recomendación pendiente: cambiar la rama predeterminada del repo en GitHub a **`main`** o borrar
  la rama `claude/exciting-einstein-BexEa` cuando ya no haga falta, para que no vuelva a pisar
  la imagen `latest`.

### Sesión 9 — 2026-06-09 (artwork local tipo Jellyfin)
- A partir del ejemplo del usuario (`poster`, `fanart`, `clearlogo`, `season01-poster`,
  `season-specials-poster`, `theme.mp3`), se amplió la escritura de metadata local.
- Al mover películas/series, la app ahora intenta descargar desde TMDB:
  `poster.jpg`, `fanart.jpg`, `clearlogo.png` y, para series, `seasonXX-poster.jpg`
  o `season-specials-poster.jpg` cuando la temporada es 0.
- También se enriquecen los `.nfo` con `<plot>` además del `uniqueid` de TMDB.
- Pendiente: `theme.mp3` no sale de TMDB. Para eso hace falta otra estrategia:
  subir/copiar un archivo manualmente, buscar una fuente externa, o integrarlo como opción
  avanzada más adelante.

### Sesión 10 — 2026-06-09 (destino existente y relojes atascados)
- Si un episodio/película ya tiene el archivo final exacto en destino, la app **ya no lo mueve**
  ni crea copia `(2)`. Lo deja pendiente con aviso para que el usuario borre el pendiente de
  descargas si Jellyfin ya está correcto.
- En Series se añadió botón para **borrar de golpe los pendientes que ya existen en destino**.
  Solo borra la descarga pendiente; no toca la biblioteca.
- Si un item queda con reloj (`processing`) demasiado tiempo, aparece una acción para devolverlo
  a pendiente y poder decidir de nuevo. Al actualizar/reiniciar contenedor, `reset_processing`
  también recupera esos casos.
- Se optimizó la descarga de artwork: no consulta imágenes de TMDB por cada episodio cuando los
  archivos locales (`poster`, `fanart`, `clearlogo`, temporada) ya existen.

### Sesión 11 — 2026-06-09 (feedback visible de "Buscar ahora")
- El botón **Buscar ahora** antes corría el escaneo en segundo plano pero no mostraba estado,
  así que parecía que no hacía nada.
- Se añadió estado persistido en BD (`scan_status`): muestra banner global de
  "Buscando archivos nuevos", auto-recarga mientras corre y luego indica cuántos archivos revisó
  y si encontró pendientes nuevos.
- El botón queda deshabilitado y cambia a "Buscando..." mientras hay un escaneo manual en curso,
  para evitar doble-clicks y dar feedback inmediato.
