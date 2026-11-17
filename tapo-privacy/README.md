# Приватный режим камер Tapo на Home Assistant OS

Аддон HA OS и вспомогательный скрипт для локального управления lens mask
(приватным режимом) камер Tapo. RTSP через go2rtc при этом остаётся как есть:
аддон ходит в камеру по HTTPS на 443, go2rtc — по RTSP на 554. Это два
независимых канала к одному устройству.

## Структура

```
tapo-privacy/
├── repository.yaml          # описание локального репозитория аддонов
├── tapo_privacy/            # сам аддон
│   ├── config.yaml
│   ├── Dockerfile
│   └── service.py
├── tapo_privacy_cli.py      # разовая проверка камеры с компьютера
└── README.md
```

## Шаг 1. Проверить, что камера умеет приватный режим

Это лучше сделать не на HA OS, а с любого компьютера в той же сети, где стоит
Python. На HA OS с этим отдельная возня, а проверка нужна один раз.

```bash
pip install pytapo
python3 tapo_privacy_cli.py --host 192.168.1.50 --password 'пароль' --state status
```

Ответ `приватный режим: off` — камера поддерживает. Ошибка
`Invalid authentication data` или `-40211` — нужен другой пароль:

```bash
python3 tapo_privacy_cli.py --host 192.168.1.50 --password 'пароль камеры' \
    --cloud-password 'пароль аккаунта TP-Link' --state status
```

Включить и выключить:

```bash
python3 tapo_privacy_cli.py --host 192.168.1.50 --password 'пароль' --state on
python3 tapo_privacy_cli.py --host 192.168.1.50 --password 'пароль' --state off
```

## Шаг 2. Положить аддон в HA OS

Каталог `tapo-privacy` целиком закидывается в папку `addons` на HA OS. Дальше
он станет локальным репозиторием аддонов.

**Через Samba.** Если установлен аддон Samba share, папка видна как `addons`:

```
\\homeassistant\addons\tapo-privacy\
```

**Через SSH.** Если установлен аддон Advanced SSH & Web Terminal и в его
настройках включён `protection_mode: false`:

```bash
scp -r tapo-privacy root@homeassistant.local:/addons/
```

**Через файловый редактор.** В настройках аддона File editor включите
`Enforce Basepath: off`, тогда в нём доступна вся файловая система, и папку
можно создать прямо в `/addons`.

После копирования: **Настройки → Дополнения → ⋮ → Проверить наличие обновлений**,
затем в правом верхнем углу **Магазин аддонов → ⋮ → Репозитории** и добавить
локальный. Если `tapo-privacy` лежит в `/addons`, HA OS покажет его в магазине
сам, без добавления URL.

## Шаг 3. Настроить

В карточке аддона → вкладка «Настройки»:

```yaml
cameras:
  - name: kitchen
    host: 192.168.1.50
    password: пароль камеры
  - name: hall
    host: 192.168.1.51
    password: пароль камеры
    cloud_password: пароль аккаунта TP-Link
timeout: 10
token: ""
```

`token` — общий секрет для запросов. Если пусто, аддон принимает всех, кто
дотянулся до порта 8099. Если задан — требуется заголовок `X-Auth-Token: <token>`
или `Authorization: Bearer <token>`. Рекомендую задать непустой.

У аддона `host_network: true`, поэтому он слушает порт 8099 прямо на HA OS и
виден по адресу HA — как раз чтобы `rest_command` до него доставал. Заодно ему
не нужен отдельный проброс сети до камер.

## Шаг 4. Запустить и проверить

Запустить аддон, затем в логе появится строка вида:

```
tapo-privacy слушает 0.0.0.0:8099, камеры: kitchen, hall
```

Проверка из терминала HA OS:

```bash
curl "http://127.0.0.1:8099/status?camera=kitchen"
# {"data": [{"camera": "kitchen", "privacy_enabled": false}]}
```

Если камера недоступна — в ответе будет `{"camera": "kitchen", "error": "камера не ответила за 10 с"}`,
и это нормально: выключенная камера не отвечает, а аддон дальше не висит.

## Шаг 5. Подключить к Home Assistant

В `configuration.yaml`:

```yaml
rest_command:
  tapo_privacy_on:
    url: "http://127.0.0.1:8099/privacy?camera={{ camera }}&state=on"
    method: post
    headers:
      X-Auth-Token: !secret tapo_privacy_token
    timeout: 20

  tapo_privacy_off:
    url: "http://127.0.0.1:8099/privacy?camera={{ camera }}&state=off"
    method: post
    headers:
      X-Auth-Token: !secret tapo_privacy_token
    timeout: 20

  tapo_privacy_status:
    url: "http://127.0.0.1:8099/status?camera={{ camera }}"
    method: get
    headers:
      X-Auth-Token: !secret tapo_privacy_token
    timeout: 20
```

В `secrets.yaml`:

```yaml
tapo_privacy_token: тот-же-токен-что-в-аддоне
```

Переключатель:

```yaml
switch:
  - platform: template
    switches:
      tapo_kitchen_privacy:
        friendly_name: "Кухня — приватный режим"
        optimistic: true
        turn_on:
          - action: rest_command.tapo_privacy_on
            data:
              camera: kitchen
        turn_off:
          - action: rest_command.tapo_privacy_off
            data:
              camera: kitchen
```

Дальше — автоматизации как обычно: включать приватный режим, когда кто-то дома,
выключать при уходе.

## Что происходит с go2rtc

Приватный режим включается на самой камере. Она перестаёт отдавать видео, и
RTSP-поток в go2rtc обрывается сам, клиенты получают ошибку вместо кадра.
Конфиг go2rtc трогать не нужно, и настройки RTSP в приложении Tapo не меняются.

Разница с удалением стрима из go2rtc (`DELETE /api/streams`) принципиальная:
там камера продолжает снимать и отдавать поток всем напрямую — Frigate, NVR,
приложению Tapo. Здесь видео не покидает камеру вовсе.

## Чего этот аддон не делает

**Не выключает микрофон.** `setPrivacyMode` гасит только видео, аудио из потока
никуда не девается. Если камера стоит там, где говорят лишнее, этого мало.

**Не блокирует камеру.** Это шторка, а не замок: кто знает пароль, включит
приватный режим обратно. Для полного покоя нужна умная розетка (у C100) или
выключение PoE-порта на коммутаторе (у C500).

**Не требует интернета.** Всё работает локально, включая авторизацию. Облако
TP-Link нужно только как источник пароля при входе.
