# MTS Business API → Zabbix: мониторинг пакетов и балансов корпоративных номеров

Мониторинг остатков минут / SMS / интернета и балансов лицевых счетов корпоративной
подписки МТС через [МТС Бизнес API](https://support.mts.ru/mts_biznes_api) в Zabbix 7.x.

- Один скрипт-обёртка API (только stdlib, Python 3.6+, никаких зависимостей)
- Автоматическая LLD: все номера подписки обнаруживаются сами, имена берутся
  из комментариев к номерам в ЛК МТС Бизнес
- Кэш + фоновый воркер + rate-limit: API-лимиты и HTTP 429 не страшны
- Готовый шаблон Zabbix с триггерами и настраиваемыми порогами

## Состав репозитория

| Файл | Назначение |
|---|---|
| `scripts/mts_balance.py` | скрипт-обёртка API + все проверки для Zabbix |
| `template_mts_business_api_74.yaml` | шаблон Zabbix 7.4 (LLD, item/trigger-прототипы, макросы) |
| `userparameter_mts.conf` | UserParameter'ы для zabbix-agent2 |
| `mts.conf.example` | шаблон конфига (ключи API вынесены из кода) |
| `install_mts_agent.sh` | установка на хост с zabbix-agent2 (запускать от root) |

## Что мониторится по каждому номеру

| Item | Источник (МТС API) | Ед. | Порог по умолчанию |
|---|---|---|---|
| Баланс лицевого счёта | `CheckBalanceByAccount` | RUB | `< {$LLT.BAL_WARN}` (−30000) |
| Остаток минут | `ValidityInfo`, счётчики SECOND/MINUTE | мин | `< {$LLT.VOICE_WARN_MIN}` (60) |
| Остаток SMS | `ValidityInfo`, счётчики ITEM | шт | `< {$LLT.SMS_WARN}` (50) |
| Остаток интернета | `ValidityInfo` h2oProfile, месячный quota (макс. BTQ) | ГБ (двоичные) | `< {$LLT.NET_WARN_PCT}%` от пакета |
| Остаток интернета, % | BTQ vs reminder | % | двойное подтверждение 13 ч |

LLD-макросы: `{#MSISDN}`, `{#ACCOUNT}`, `{#REGION}`, `{#NAME}`, `{#NAME_SHORT}`
(имена — из `GetCommentsByMSISDN`, то есть из «названий симок» в ЛК МТС).

## Архитектура

```
zabbix-agent2 ──UserParameter──> mts_balance.py ──кэш /var/tmp/mts_api_*.json──> Zabbix
                                      │
                                      └─ фоновый воркер (flock) ──rate limit──> api.mts.ru
```

- Чек никогда не ждёт API: читает файл кэша; если устарел — ставит задачу в
  очередь и поднимает один фоновый воркер. Ответ агенту моментальный.
- Воркер сериализует все запросы через flock + `min_interval` — 429 исключены.
- Баланс: номер резолвится в accountNo по иерархии (`HierarchyStructure`);
  у десятков номеров обычно несколько ЛиС — запросов в разы меньше, чем по номеру.
- Ошибки API не затирают последние хорошие данные (`.err`-метки).

## Установка

### 1. Хост с zabbix-agent2 (например выделенный сервер мониторинга телефонии)

```
git clone https://github.com/volodinaleksei/zabbix_mts_balance_api.git
cd zabbix_mts_balance_api
cp mts.conf.example mts.conf        # вписать client_id / client_secret из ЛК МТС Бизнес
sudo bash install_mts_agent.sh
```

Скрипт положит обёртку в `/etc/zabbix/scripts/`, конфиг в `mts.conf` там же
(права `root:zabbix 640`), UserParameter — в `/etc/zabbix/zabbix_agent2.d/`,
перезапустит агент и сам проверит, что LLD отвечает.

### 2. Сервер Zabbix

1. Импорт `template_mts_business_api_74.yaml` (группа Teletraffic).
2. Привязать шаблон к хосту с агентом из п.1.
3. Дождаться discovery (раз в сутки) или запустить вручную у правила
   «MTS: номера подписки».

### 3. Проверка вручную

```
su -s /bin/sh zabbix -c '/etc/zabbix/scripts/mts_balance.py discovery' | head -c 300
su -s /bin/sh zabbix -c '/etc/zabbix/scripts/mts_balance.py net 79XXXXXXXXXX'
zabbix_get -s <agent> -k 'mts.voice[79XXXXXXXXXX]'
```

## Машиночитаемые режимы скрипта

| Команда | Вывод |
|---|---|
| `mts_balance.py discovery` | LLD JSON (все номера + имена) |
| `mts_balance.py comments` | JSON маппинг номер → комментарий ЛК |
| `mts_balance.py balance <msisdn>` | остаток ЛиС, RUB |
| `mts_balance.py voice <msisdn>` | минуты |
| `mts_balance.py sms <msisdn>` | SMS |
| `mts_balance.py net <msisdn>` | интернет, ГБ (двоичные) |
| `mts_balance.py netpct <msisdn>` | интернет, % от пакета |
| `mts_balance.py worker` | фоновый рефрешер (поднимается сам) |

## Важные тонкости (проверено на живых ответах API)

- **Токен**: Basic Auth (`client_id:client_secret`) + form `grant_type=client_credentials`
  на `https://api.mts.ru/token`; живёт 3600 с, скрипт обновляет сам.
- **`HierarchyStructure`** без параметров режет список (`"href":"hasMore"`) —
  нужен `?pageNum=1&pageSize=500`; фильтры вида `productSerialNumber=` API игнорирует.
- **Баланс** — по `CheckBalanceByAccount` (не по MSISDN): меньше запросов, нет 429.
- **Интернет** в `h2oProfile`: quota-блоки лежат рекурсивно в
  `bundledProductSpecification[].productSpecCharacteristic[name=quota]` — чтение
  только верхнего уровня молча вернёт пустоту. Значения в КиБ; «310 ГБ» из ЛК =
  310 **двоичных** GiB. Основной пакет = блок с `Rotate=1ExtendedMonth`.
- **Ночная перегенерация биллинга**: МТС периодически отдаёт ValidityInfo без
  месячного quota-блока (не только ночью). Фallback — последний хороший снимок
  из side-кэша `netgood_<msisdn>`; одиночные провалы гасятся двойным
  подтверждением триггера (`count(...,13h)>=2`).
- **Безлимитные тарифы**: у активного «Пакет GPRS-трафика для безлимитных опций»
  месячного quota-блока нет никогда — в биллинге только суточные shaping-ступеньки
  (100 КБ / 1 МБ / 500 МБ). Скрипт отдаёт для таких спецзначение **9999 ГБ**
  и netpct = 100%, чтобы не показывать ложные «0.49 ГБ».
- **Городская телефония** (стационарные номера подписки) может не иметь пакетов
  вообще — нули там корректны.
- Переименование item-прототипов НЕ переименовывает уже созданные items
  (ключи те же, история сохраняется; ради новых имён items LLD нужно пересоздать).

## Безопасность

- Ключи API живут только в `mts.conf` (`/etc/zabbix/scripts/mts.conf`,
  права `640 root:zabbix`). В репозиторий они не попадают — `.gitignore` бережёт.
- Скрипт не делает ничего, кроме GET/POST на `api.mts.ru`, и только чтения данных.

## Лицензия

MIT — используйте свободно. Автор не связан с ПАО «МТС»; МТС Бизнес API —
товарный знак правообладателя.
