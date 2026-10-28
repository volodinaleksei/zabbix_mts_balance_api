#!/bin/bash
# Установка МТС-мониторинга на хост с zabbix-agent2 (у нас — mts-mon).
# Запускать от root из корня репозитория:  sudo bash install_mts_agent.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"

install -d -o root -g zabbix -m 750 /etc/zabbix/scripts
install -o root -g zabbix -m 750 "$SRC/scripts/mts_balance.py" /etc/zabbix/scripts/mts_balance.py

# Конфиг с ключами API: берём mts.conf рядом со скриптом, если он есть;
# иначе — если на машине уже есть рабочий конфиг, не трогаем его.
if [ -f "$SRC/mts.conf" ]; then
    install -o root -g zabbix -m 640 "$SRC/mts.conf" /etc/zabbix/scripts/mts.conf
elif [ ! -f /etc/zabbix/scripts/mts.conf ]; then
    install -o root -g zabbix -m 640 "$SRC/mts.conf.example" /etc/zabbix/scripts/mts.conf
    echo "ВНИМАНИЕ: /etc/zabbix/scripts/mts.conf создан из примера — впишите client_id/client_secret."
fi

install -o root -g root  -m 644 "$SRC/userparameter_mts.conf" /etc/zabbix/zabbix_agent2.d/userparameter_mts.conf

systemctl restart zabbix-agent2
sleep 1
systemctl is-active zabbix-agent2

# самопроверка: агент должен вернуть LLD JSON
su -s /bin/sh zabbix -c '/etc/zabbix/scripts/mts_balance.py discovery | head -c 120; echo'
echo OK
