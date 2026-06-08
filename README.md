# 📦 NAS Organizer

Organizador **semi-automático** de descargas para tu NAS Synology + Jellyfin.

Vigila la carpeta donde JDownloader deja los archivos (con nombres feos y sin
metadatos), los identifica (película / serie / música), busca su título, año y
póster, y te muestra una **bandeja de revisión** sencilla en el navegador. Tú solo
pulsas **✅ Confirmar y mover** y el archivo se renombra, se mueve a la carpeta correcta
de Jellyfin y Jellyfin se actualiza solo.

> No toca nada de JDownloader: funciona de forma independiente vigilando la carpeta.

---

## ✨ Qué hace

- 🔎 **Detecta** automáticamente lo nuevo en tu carpeta de descargas.
- 🏷️ **Identifica** películas y series con TheMovieDB (título, año, póster, sinopsis) y
  música por sus etiquetas / MusicBrainz.
- 🎵 **Música editable**: corrige artista, álbum, título y nº de pista a mano, o búscalos en
  MusicBrainz desde la propia web.
- 🗂️ Pestañas separadas: **Películas · Series · Música**.
- ✅ Tú **confirmas con un clic**; también puedes **Editar** (buscar el título correcto),
  **Cambiar tipo**, **Omitir** o **Eliminar**.
- 📁 **Renombra y mueve** con la estructura que Jellyfin entiende:
  - Películas → `Películas/Título (Año)/Título (Año).mkv`
  - Series → `Series/Título/Season 01/Título S01E02.mkv`
  - Música → `Música/Artista/Álbum/01 - Canción.mp3`
  - Arrastra los **subtítulos** junto al vídeo.
- 🔄 **Refresca Jellyfin** automáticamente (escaneo incremental) tras cada movimiento, con
  un botón aparte para **Actualizar todo** cuando lo necesites.
- ⚙️ Todas las rutas, URLs y claves son **editables desde la web** (pestaña Ajustes).

---

## 🚀 Instalación en Synology (Container Manager / Docker)

### 1. Requisitos previos
- **API key gratuita de TheMovieDB**: crea una cuenta en
  <https://www.themoviedb.org/settings/api> y copia tu *API Key (v3 auth)*.
- **API key de Jellyfin**: en Jellyfin → *Panel de control → API Keys → +*. Apunta también
  la URL de Jellyfin (ej. `http://192.168.1.10:8096`).

### 2. Descarga el proyecto al NAS
Copia esta carpeta al NAS, por ejemplo a `/volume1/docker/nas-organizer`.

### 3. Ajusta las rutas en `docker-compose.yml`
Edita **solo la parte izquierda** de `volumes` para que apunte a tus carpetas reales:

```yaml
    volumes:
      - /volume1/downloads:/downloads      # carpeta donde descarga JDownloader
      - /volume1/media:/media              # raíz de tu biblioteca de Jellyfin
      - ./data:/data                       # datos de la app (no borrar)
```

> Dentro del contenedor, las películas irán a `/media/Películas`, las series a
> `/media/Series` y la música a `/media/Música`. Puedes cambiar estos nombres en
> **Ajustes**. Asegúrate de que coincidan con las carpetas que Jellyfin ya tiene
> configuradas como bibliotecas.

### 4. Levanta el contenedor
Por terminal (SSH):

```bash
cd /volume1/docker/nas-organizer
docker compose up -d --build
```

O desde **Container Manager → Proyecto → Crear**, apuntando a esta carpeta.

### 5. Abre la interfaz
Desde cualquier navegador (PC o móvil en tu red):

```
http://IP-DE-TU-NAS:8678
```

### 6. Configura (una sola vez)
Entra a la pestaña **Ajustes** y rellena:
- Tu **API key de TMDB**.
- La **URL** y **API key de Jellyfin**.
- Revisa que las carpetas sean correctas.

¡Listo! Descarga algo con JDownloader y aparecerá en la bandeja para revisar.

---

## 🕹️ Uso diario

1. JDownloader termina una descarga.
2. En unos segundos aparece en la pestaña correspondiente (o pulsa **🔄 Buscar ahora**).
3. Revisa la coincidencia y el póster.
   - ¿Correcto? → **✅ Confirmar y mover**.
   - ¿Mal identificado? → **✏️ Editar / buscar** y elige el título correcto.
   - ¿Tipo equivocado? → **Cambiar tipo** (p.ej. de Película a Serie).
4. El archivo se ordena solo y Jellyfin se actualiza.

---

## 🔧 Notas técnicas

- Stack: **FastAPI + Jinja2 + SQLite** (sin base de datos externa ni build de frontend).
- Identificación de nombres con **guessit**, metadatos de vídeo con **TheMovieDB**,
  música con **mutagen + MusicBrainz**.
- La app **espera ~60 s** a que un archivo deje de cambiar antes de procesarlo, para no
  tocar descargas a medias. Ignora archivos `.part`, `.tmp`, etc.
- La música usa primero las etiquetas del archivo; si faltan, puedes editarlas a mano o
  buscarlas en MusicBrainz desde **✏️ Editar etiquetas** en cada canción.
- Cambia el puerto `8678` en `docker-compose.yml` si ya está ocupado.

## 🛠️ Desarrollo local (sin Docker)

```bash
pip install -r requirements.txt
export NAS_DOWNLOADS_DIR=/ruta/descargas NAS_MOVIES_DIR=/ruta/Películas
uvicorn app.main:app --reload --port 8678
```
