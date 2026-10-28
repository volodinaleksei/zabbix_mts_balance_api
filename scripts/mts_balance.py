#!/usr/bin/env python3
"""
МТС Бизнес API -> Zabbix: мониторинг остатков пакетов и баланса по номерам.
Версия 2: кэш + фоновый воркер-рефрешер (лечит HTTP 429 и таймауты агента).

Использование (вызывается Zabbix через UserParameter):
    mts_balance.py discovery          -> LLD JSON: все номера подписки
    mts_balance.py balance  <msisdn>  -> остаток по ЛиС, руб (float)
    mts_balance.py voice    <msisdn>  -> остаток минут, мин (float)
    mts_balance.py sms      <msisdn>  -> остаток SMS, шт
    mts_balance.py net      <msisdn>  -> остаток интернета, ГБ (двоичные)
    mts_balance.py netpct   <msisdn>  -> остаток интернета, % от выданного

Архитектура:
- Чек никогда не ждёт API в разгар опроса: читает файл кэша (даже устаревший),
  а устаревание ставит задачу в очередь pending и поднимает ОДИН фоновый
  процесс-воркер (pid-лок). Проверка агента моментальная.
- Воркер обрабатывает очередь сериально, все HTTP-запросы проходят через
  rate_limit (flock + min_interval) -> 429 от МТС исключены.
- Баланс: номер резолвится в accountNo по иерархии, баланс берётся
  CheckBalanceByAccount. У 81 номера ~4 ЛиС => 4 запроса вместо 81.
- Ошибки API пишутся в .err-метку и НЕ кэшируются как данные; чек отдаёт
  последние хорошие данные, свежая ошибка показывается только когда данных
  нет вовсе.

Только stdlib. Python 3.6+.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------- настройки ----------
CONF = os.environ.get("MTS_CONF", "/etc/zabbix/scripts/mts.conf")
FALLBACK = {
    "client_id": "",
    "client_secret": "",
    "cache_dir": "/var/tmp",
    "cache_ttl": "300",
    "min_interval": "1.5",
    "page_size": "500",
}


def load_conf():
    conf = dict(FALLBACK)
    try:
        with open(CONF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    except OSError:
        pass
    return conf


API = "https://api.mts.ru"
TIMEOUT = 30

# режим воркера (ставится из main)
WORKER = False


class ApiError(Exception):
    pass


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


# ---------- rate limit между процессами ----------
def rate_limit(conf):
    """Не чаще 1 HTTP-запроса в min_interval сек глобально (flock-очередь)."""
    import fcntl
    min_int = float(conf.get("min_interval", "1.5"))
    path = os.path.join(conf["cache_dir"], "mts_api_rate")
    try:
        with open(path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                last = float(f.read().strip() or 0)
            except ValueError:
                last = 0.0
            now = time.time()
            wait = max(0.0, last + min_int - now)
            f.seek(0)
            f.truncate()
            f.write("%.3f" % (now + wait))
        if wait > 0:
            time.sleep(wait)
    except OSError:
        pass


def http(url, data=None, headers=None, soft=False, retries=4):
    """soft=True -> ApiError вместо die(); ретраи 429/5xx с backoff."""
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503) and attempt < retries:
                attempt += 1
                retry_after = e.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 0
                except ValueError:
                    delay = 0
                time.sleep(min(90, max(delay, 5.0 * attempt * attempt)))
                continue
            msg = "HTTP %s %s: %s" % (e.code, url.split("?")[0], body)
            if soft:
                raise ApiError(msg)
            die(msg)
        except ApiError:
            raise
        except Exception as e:
            if soft:
                raise ApiError("%s: %s" % (url.split("?")[0], e))
            die("%s: %s" % (url.split("?")[0], e))


def api_get(conf, token, url, soft=False):
    rate_limit(conf)
    return http(url, headers={"Authorization": "Bearer " + token,
                              "Accept": "application/json"}, soft=soft)


def api_post(conf, token, url, payload, soft=False):
    rate_limit(conf)
    return http(url, data=json.dumps(payload).encode(),
                headers={"Authorization": "Bearer " + token,
                         "Content-Type": "application/json",
                         "Accept": "application/json"}, soft=soft)


# ---------- токен с кэшем ----------
def get_token(conf):
    cache = os.path.join(conf["cache_dir"], "mts_api_token.json")
    try:
        with open(cache) as f:
            t = json.load(f)
        if t.get("expires_at", 0) > time.time() + 30:
            return t["access_token"]
    except (OSError, ValueError, KeyError):
        pass

    import base64
    basic = base64.b64encode(
        ("%s:%s" % (conf["client_id"], conf["client_secret"])).encode()
    ).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    r = http(
        API + "/token",
        data=body,
        headers={
            "Authorization": "Basic " + basic,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        soft=WORKER,
    )
    if "access_token" not in r:
        raise ApiError("no access_token in token response: %s" % str(r)[:200])
    r["expires_at"] = time.time() + int(r.get("expires_in", 3600)) - 60
    try:
        tmp = cache + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(r, f)
        os.replace(tmp, cache)
        os.chmod(cache, 0o600)
    except OSError:
        pass
    return r["access_token"]


# ---------- кэш ответов ----------
def cache_path(conf, key):
    return os.path.join(conf["cache_dir"], "mts_api_%s.json" % key.replace("/", "_"))


def err_path(conf, key):
    return cache_path(conf, key) + ".err"


def _write_cache(path, data):
    try:
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def load_cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_err(conf, key):
    try:
        with open(err_path(conf, key)) as f:
            msg, ts = f.read().split("\t", 1)
        return msg, float(ts)
    except (OSError, ValueError):
        return None, 0.0


def write_err(conf, key, msg):
    try:
        with open(err_path(conf, key), "w") as f:
            f.write("%s\t%.3f" % (msg.replace("\n", " "), time.time()))
    except OSError:
        pass


def clear_err(conf, key):
    try:
        os.remove(err_path(conf, key))
    except OSError:
        pass


# ---------- очередь задач и фоновый воркер ----------
def queue_files(conf):
    return (os.path.join(conf["cache_dir"], "mts_refresh_queue"),
            os.path.join(conf["cache_dir"], "mts_refresh.pid"))


def enqueue(conf, job):
    """job = 'MODE MSISDN' | 'discovery' | 'account ACCOUNTNO'; дубликаты убираются."""
    import fcntl
    qpath, _ = queue_files(conf)
    try:
        with open(qpath, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            jobs = [l.strip() for l in f if l.strip()]
            if job not in jobs:
                jobs.append(job)
            f.seek(0)
            f.truncate()
            f.write("\n".join(jobs) + "\n")
    except OSError:
        pass


def dequeue_all(conf):
    import fcntl
    qpath, _ = queue_files(conf)
    try:
        with open(qpath, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            jobs = [l.strip() for l in f if l.strip()]
            f.seek(0)
            f.truncate()
        return jobs
    except OSError:
        return []


def spawn_worker(conf):
    """Не более одного воркера: pid-файл + os.kill(pid, 0)."""
    import subprocess
    _, lockpath = queue_files(conf)
    try:
        try:
            with open(lockpath) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return  # воркер уже работает
        except (OSError, ValueError):
            pass  # ложа нет / процесс мёртв — забираем lock
        tmp = lockpath + ".new.%d" % os.getpid()
        with open(tmp, "w") as f:
            f.write(str(os.getpid()))
        # перечитать: гонка между проверкой и записью
        try:
            with open(lockpath) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            os.remove(tmp)
            return
        except (OSError, ValueError):
            os.replace(tmp, lockpath)
        log = open(os.path.join(conf["cache_dir"], "mts_refresh.log"), "a")
        env = dict(os.environ)
        env["MTS_BG"] = "1"
        subprocess.Popen(
            [sys.executable, os.path.abspath(sys.argv[0]), "worker"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )
    except OSError:
        pass


def job_endpoint(conf, job):
    """-> (cache_key, url); для discovery/comments url=None (воркер сам сделает запрос)."""
    parts = job.split()
    if not parts:
        return None, None
    mode = parts[0]
    if mode == "discovery":
        return "hierarchy", None
    if mode == "comments":
        return "comments", None
    if mode == "account":
        return ("bal_" + parts[1],
                API + "/b2b/v1/Bills/CheckBalanceByAccount?fields=MOAF&accountNo=" + parts[1])
    msisdn = parts[1] if len(parts) > 1 else ""
    if not msisdn:
        return None, None
    if mode == "balance":
        acc = get_account_map(conf).get(msisdn)
        if acc:
            return ("bal_" + acc,
                    API + "/b2b/v1/Bills/CheckBalanceByAccount?fields=MOAF&accountNo=" + acc)
        return ("bal_" + msisdn,
                API + "/b2b/v1/Bills/CheckBalanceByMSISDN"
                "?fields=MOAF&characteristic.name=MSISDN&characteristic.value=" + msisdn)
    # voice/sms/net/netpct => ValidityInfo
    return ("val_" + msisdn,
            API + "/b2b/v1/Bills/ValidityInfo"
            "?fields=MOAF,forisCounters,h2oProfile"
            "&customerAccount.accountNo=" + msisdn +
            "&customerAccount.productRelationship.product.productLine.name=Counters")


def run_worker(conf):
    """Фоновый рефрешер: очередь сериально, всё через rate_limit (в api_get)."""
    jobs = dequeue_all(conf)
    for _round in range(3):
        if not jobs:
            break
        for job in jobs:
            key = None
            try:
                key, url = job_endpoint(conf, job)
                if key is None:
                    continue
                token = get_token(conf)
                if key == "comments":
                    # иерархия обязательна: из неё берётся список номеров для пачки
                    if load_cache(cache_path(conf, "hierarchy")) is None:
                        hier_url = (API + "/b2b/v1/Service/HierarchyStructure?pageNum=1&pageSize="
                                    + conf.get("page_size", "500"))
                        _write_cache(cache_path(conf, "hierarchy"),
                                     api_get(conf, token, hier_url, soft=True))
                    refresh_comments(conf, token)
                    clear_err(conf, key)
                    continue
                if url is None:
                    url = (API + "/b2b/v1/Service/HierarchyStructure?pageNum=1&pageSize="
                           + conf.get("page_size", "500"))
                data = api_get(conf, token, url, soft=True)
                _write_cache(cache_path(conf, key), data)
                clear_err(conf, key)
                if key == "hierarchy":
                    # список номеров изменился — обновляем комментарии заодно
                    enqueue(conf, "comments")
            except ApiError as e:
                write_err(conf, key or "worker", str(e))
            except Exception as e:  # noqa: BLE001
                write_err(conf, key or "worker", "%s: %s" % (job, e))
        jobs = dequeue_all(conf)
    _, lockpath = queue_files(conf)
    try:
        os.remove(lockpath)
    except OSError:
        pass


# ---------- чек: чтение кэша + постановка в очередь ----------
def cached_value(conf, key, job=None):
    ttl = int(conf.get("cache_ttl", "300"))
    path = cache_path(conf, key)
    data = load_cache(path)

    if data is not None:
        try:
            stale = (time.time() - os.path.getmtime(path)) >= ttl
        except OSError:
            stale = True
    else:
        stale = True

    if stale and job and not WORKER:
        enqueue(conf, job)
        spawn_worker(conf)

    if data is None:
        # первый запуск: подождём, пока воркер наполнит
        deadline = time.time() + 25
        while data is None and time.time() < deadline:
            time.sleep(1)
            data = load_cache(path)

    if data is None:
        emsg, ets = read_err(conf, key)
        if emsg and (time.time() - ets) < 300:
            die("mts API: " + emsg)
        die("mts: data is updating in background, retry in a minute")
    return data


# ---------- комментарии к номерам (название симки из ЛК) ----------
COMMENTS_BATCH = 300


def refresh_comments(conf, token):
    """POST GetCommentsByMSISDN пачками по <=300 номеров; кэш key='comments'.

    Формат кэша: {msisdn: comment_text}. Пустой ответ допустим (комментариев нет).
    """
    msisdns = sorted(get_account_map(conf).keys())
    if not msisdns:
        # иерархии ещё нет — не пишем пустой кэш, дождёмся discovery
        raise ApiError("comments: hierarchy map empty")
    out = {}
    for i in range(0, len(msisdns), COMMENTS_BATCH):
        batch = msisdns[i:i + COMMENTS_BATCH]
        data = api_post(conf, token,
                        API + "/b2b/v1/Service/GetCommentsByMSISDN",
                        {"msisdns": batch}, soft=True)
        for row in data if isinstance(data, list) else []:
            m = row.get("msisdn", "")
            c = (row.get("comment") or "").strip()
            if m and c:
                out[m] = c
    _write_cache(cache_path(conf, "comments"), out)


def get_comment_map(conf):
    data = load_cache(cache_path(conf, "comments"))
    return data if isinstance(data, dict) else {}


# ---------- структура абонента ----------
def get_account_map(conf):
    tree = load_cache(cache_path(conf, "hierarchy"))
    amap = {}
    if tree is None:
        return amap
    for node in tree if isinstance(tree, list) else [tree]:
        for pr in node.get("partyRole", []) or []:
            for ca in pr.get("customerAccount", []) or []:
                acc = ca.get("accountNo", "")
                for rel in ca.get("productRelationship", []) or []:
                    if rel.get("description") != "MSISDN":
                        continue
                    msisdn = (rel.get("product") or {}).get("productSerialNumber", "")
                    if len(msisdn) == 11:
                        amap[msisdn] = acc
    return amap


def discovery(conf):
    tree = cached_value(conf, "hierarchy", "discovery")
    # комментарии (названия симок) — отдельная задача; после обновления иерархии
    # воркер сам поставит 'comments', но на всякий случай страхуемся
    enqueue(conf, "comments")
    spawn_worker(conf)
    cmap = get_comment_map(conf)
    seen = {}
    accounts_done = set()
    for node in tree if isinstance(tree, list) else [tree]:
        for pr in node.get("partyRole", []) or []:
            for ca in pr.get("customerAccount", []) or []:
                acc = ca.get("accountNo", "")
                if acc and acc not in accounts_done:
                    enqueue(conf, "account " + acc)
                    accounts_done.add(acc)
                for rel in ca.get("productRelationship", []) or []:
                    if rel.get("description") != "MSISDN":
                        continue
                    p = rel.get("product") or {}
                    msisdn = p.get("productSerialNumber", "")
                    if len(msisdn) == 11 and msisdn not in seen:
                        cmt = cmap.get(msisdn, "")
                        # {#NAME} не должен быть пустым: fallback = номер
                        name = cmt if cmt else msisdn
                        seen[msisdn] = {
                            "{#MSISDN}": msisdn,
                            "{#ACCOUNT}": acc,
                            "{#REGION}": (p.get("description") or "").replace(" ", "_"),
                            "{#NAME}": name,
                            "{#NAME_SHORT}": (name[:32] + "…") if len(name) > 33 else name,
                        }
    print(json.dumps({"data": list(seen.values())}, ensure_ascii=False))


# ---------- балансы ----------
def get_balance(conf, msisdn):
    acc = get_account_map(conf).get(msisdn)
    if acc:
        key, job = "bal_" + acc, "account " + acc
    else:
        key, job = "bal_" + msisdn, "balance " + msisdn
    data = cached_value(conf, key, job)
    for a in data if isinstance(data, list) else [data]:
        for b in a.get("customerAccountBalance", []) or []:
            amt = b.get("remainedAmount", {}).get("amount")
            if amt is not None:
                return float(amt)
    die("no balance in response for %s" % msisdn)


# ---------- пакеты (ValidityInfo) ----------
def get_counters(conf, msisdn):
    data = cached_value(conf, "val_" + msisdn, "voice " + msisdn)

    voice = sms = 0          # сек; шт

    for block in data if isinstance(data, list) else [data]:
        for ca in block.get("customerAccount", []) or []:
            for rel in ca.get("productRelationship", []) or []:
                prod = rel.get("product") or {}
                unit = ((prod.get("productPrice") or [{}])[0]
                        .get("unitOfMeasure", "") or "")
                spec = prod.get("productSpecification") or {}
                cur = 0.0
                for ch in spec.get("productSpecCharacteristic", []) or []:
                    for v in ch.get("prodSpecCharacteristicValue", []) or []:
                        if v.get("valueType") == "CurrentValue":
                            try:
                                cur = float(v.get("value", 0))
                            except (TypeError, ValueError):
                                cur = 0.0
                if unit == "SECOND":
                    voice += cur
                elif unit == "MINUTE":
                    voice += cur * 60
                elif unit == "ITEM":
                    sms += cur

    # h2oProfile: quota-блоки в КБ; дедуп по (global_code, Rotate.At).
    # Основной пакет = месячный блок (Rotate=1ExtendedMonth) с макс. BTQ.
    # ПИТОЛ: ночью (~00:00–07:00 МСК) биллинг МТС перегенерирует ответ и
    # месячный quota-блок исчезает/обнуляется. Старый выбор «макс. BTQ среди
    # всех» при этом падал на суточный блок 700 МБ (он к утру всегда съеден)
    # -> ложная тревога «интернета < 5%» каждый день. Теперь: месячный блок
    # отсутствует/обнулён -> отдаём последний хороший снимок из side-кэша.
    groups = {}
    unlimited = {"flag": False}

    def collect(o):
        if isinstance(o, dict):
            # активный «Пакет GPRS-трафика для безлимитных опций» = безлимитный
            # тариф (например 79813014799 НИС Н.Трубятчинский): в биллинге нет
            # месячного quota-блока, только суточные shaping-ступеньки.
            ps = o.get("productSpecification")
            if (isinstance(ps, dict)
                    and ps.get("name") == "Пакет GPRS-трафика для безлимитных опций"
                    and o.get("productStatus") == "Active"):
                unlimited["flag"] = True
            if o.get("name") == "quota":
                vals = {}
                for v in o.get("prodSpecCharacteristicValue", []) or []:
                    vals[v.get("valueType")] = v.get("value")
                gkey = (vals.get("global_code_1"), vals.get("Rotate.At"))
                try:
                    btq = float(vals.get("BTQ") or vals.get("BQ") or 0)
                    rem = float(vals.get("reminder")
                                if vals.get("reminder") is not None
                                else (btq - float(vals.get("Consumption") or 0)))
                except (TypeError, ValueError):
                    btq = rem = 0.0
                if btq > 0:
                    prev = groups.get(gkey)
                    if prev is None or btq > prev[0]:
                        groups[gkey] = (btq, rem, vals.get("Rotate") or "")
            for v in o.values():
                collect(v)
        elif isinstance(o, list):
            for v in o:
                collect(v)

    collect(data)
    net_rem = net_tot = 0.0
    if groups:
        monthly = [g for g in groups.values()
                   if "ExtendedMonth" in g[2] and g[0] > 0]
        if monthly:
            net_tot, net_rem, _ = max(monthly, key=lambda g: g[0])
            _save_net_good(conf, msisdn, net_rem, net_tot)
        elif unlimited["flag"]:
            # безлимитный тариф: месячного пакета нет никогда — суточные
            # ступеньки показывать бессмысленно. Спецзначение 9999 ГБ
            # (видно что это не реальный остаток), netpct = 100.
            net_tot = net_rem = 9999.0 * 1048576.0
        else:
            good = _load_net_good(conf, msisdn)
            if good is not None:
                net_tot, net_rem = good
            else:
                # нет ни месячного блока, ни кэша — старое поведение
                net_tot, net_rem = max(groups.values(), key=lambda g: g[0])[:2]
    return voice, sms, net_rem, net_tot


def _net_good_path(conf, msisdn):
    return cache_path(conf, "netgood_" + msisdn)


def _save_net_good(conf, msisdn, rem, tot):
    try:
        _write_cache(_net_good_path(conf, msisdn),
                     {"rem": rem, "tot": tot, "ts": int(time.time())})
    except OSError:
        pass


def _load_net_good(conf, msisdn):
    d = load_cache(_net_good_path(conf, msisdn))
    if not isinstance(d, dict):
        return None
    try:
        return float(d["tot"]), float(d["rem"])
    except (ValueError, KeyError):
        return None


def main():
    global WORKER
    if (len(sys.argv) > 1 and sys.argv[1] == "worker") or os.environ.get("MTS_BG") == "1":
        WORKER = True
        run_worker(load_conf())
        return
    if len(sys.argv) < 2:
        die(__doc__)
    conf = load_conf()
    mode = sys.argv[1]

    if mode == "discovery":
        discovery(conf)
        return
    if mode == "comments":
        # ручное обновление кэша комментариев (для отладки/скриптов)
        tree = cached_value(conf, "hierarchy", "discovery")
        refresh_comments(conf, get_token(conf))
        print(json.dumps(get_comment_map(conf), ensure_ascii=False))
        return
    if mode not in ("balance", "voice", "sms", "net", "netpct"):
        die("unknown mode: " + mode)
    if len(sys.argv) < 3:
        die("usage: mts_balance.py %s <msisdn>" % mode)
    msisdn = sys.argv[2]

    if mode == "balance":
        print("%.4f" % get_balance(conf, msisdn))
        return

    voice, sms, net_rem, net_tot = get_counters(conf, msisdn)
    if mode == "voice":
        print("%.2f" % (voice / 60.0))
    elif mode == "sms":
        print(int(sms))
    elif mode == "net":
        # КиБ -> binary ГБ (как в ЛК МТС: 310 ГБ = 310 GiB); Zabbix units '!ГБ'
        print("%.2f" % (net_rem / 1048576.0))
    elif mode == "netpct":
        print("%.1f" % (100.0 * net_rem / net_tot if net_tot else 0.0))


if __name__ == "__main__":
    main()
