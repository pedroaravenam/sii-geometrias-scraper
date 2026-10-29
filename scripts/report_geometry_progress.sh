#!/usr/bin/env bash
# Informa el avance de una tarea existente sin reiniciar ni modificar el scraper.
set -Eeuo pipefail

nombre="${1:?indica la tarea de omp-tarea-larga}"
[[ "$nombre" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo 'Nombre de tarea inválido' >&2; exit 2; }
dir="${OMP_TAREAS_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/omp-tareas}/$nombre"
[[ -f "$dir/progreso" && -f "$dir/cwd" ]] || { echo "No existe la tarea $nombre" >&2; exit 1; }

estado="$(systemctl --user is-active "tarea-$nombre.service" 2>/dev/null || true)"
comando="$(cat "$dir/progreso")"
if [[ -n "$comando" ]]; then
  if ! progreso="$(cd "$(cat "$dir/cwd")" && timeout 60 bash -c "$comando" | tail -n 1)"; then
    progreso='sin resumen disponible (falló el comando de progreso)'
  fi
else
  progreso='sin comando de progreso'
fi
informe="$(date '+%Y-%m-%d %H:%M') | $estado | $progreso"
printf '%s\n' "$informe" | tee -a "$dir/reporte-horario.log"
if command -v notify-send >/dev/null 2>&1; then
  notify-send "Avance SII: $nombre" "$informe" >/dev/null 2>&1 || true
fi
if [[ "$estado" != active && "$estado" != activating ]]; then
  systemctl --user disable --now "tarea-$nombre-reporte.timer" >/dev/null 2>&1 || true
fi
