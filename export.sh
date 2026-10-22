#!/usr/bin/env bash
# export.sh — CSV-выгрузка заявок для менеджера (период: с аргумента или последние 30 дней).
# Формат: id;address;slot;client_name;client_phone;status;created_at ; разделитель ';', UTF-8 + BOM.
#: Контейнер, пользователь и база — переопределяемыми переменными; умолчание базы — база продукта.
#: Файл содержит персональные данные: без `EXPORT_GPG_RECIPIENT` он остаётся незашифрованным, и скрипт
#: говорит об этом громко.
set -euo pipefail
OUT="${1:-bookings_$(date +%F).csv}"
DAYS="${2:-30}"

{
  printf '\xEF\xBB\xBF'
  echo "id;address;slot;client_name;client_phone;status;created_at"
  docker exec -i "${PG_CONT:-realty-db}" \
    psql -U "${PG_USER:-realty}" -d "${PG_DB:-realty}" -At -F';' -c "
      SELECT b.id, COALESCE(l.address,''), to_char(b.slot AT TIME ZONE 'Europe/Moscow','YYYY-MM-DD HH24:MI'),
             b.client_name, b.client_phone, b.status, b.created_at
      FROM bookings b LEFT JOIN listings l ON l.id = b.listing_id
      WHERE b.created_at >= now() - interval '${DAYS} days'
      ORDER BY b.slot;"
} > "$OUT"

target="${OUT}.gpg"
if command -v gpg >/dev/null && [[ -n "${EXPORT_GPG_RECIPIENT:-}" ]]; then
  gpg --yes --encrypt --recipient "$EXPORT_GPG_RECIPIENT" --output "$target" "$OUT"
  rm -f "$OUT"
  echo "[export] зашифровано: $target"
else
  echo "[export] ВНИМАНИЕ: файл содержит ПД и не зашифрован ($OUT). Задайте EXPORT_GPG_RECIPIENT."
fi
