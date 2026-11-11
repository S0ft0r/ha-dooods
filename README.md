# Установка DOOODS

В репозитории два компонента, и оба ставятся с одного и того же адреса:

- **интеграция** `custom_components/dooods/` — в Home Assistant, через Marketplace или HACS;
- **приложение** `local_apps/dooods2/` — в магазин приложений Home Assistant.

Адрес репозитория: `https://github.com/S0ft0r/ha-dooods`

---

## Интеграция

### Marketplace (Home Assistant 2026.11 и новее)

1. Открыть ссылку:

[![Open your Home Assistant instance and open a repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?repository=https%3A%2F%2Fgithub.com%2FS0ft0r%2Fha-dooods&category=Integration&owner=S0ft0r)

2. Home Assistant предложит добавить репозиторий — согласиться.
3. Нажать «Установить».
4. Перезапустить Home Assistant.
5. **Настройки → Устройства и службы → Добавить интеграцию → DOOODS**.

### HACS (2026.10 и старше)

1. **HACS → Интеграции → ⋮ → Пользовательские репозитории**.
2. Вставить `https://github.com/S0ft0r/ha-dooods`, тип — **Integration**, нажать «Добавить».
3. Найти DOOODS в списке и установить.
4. Перезапустить Home Assistant.
5. **Настройки → Устройства и службы → Добавить интеграцию → DOOODS**.


---

## Приложение DOOODS2

1. Открыть ссылку:

[![Open your Home Assistant instance and show the add app repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FS0ft0r%2Fha-dooods)

**или** 

1. **Настройки → Приложения → Магазин приложений**.
2. ⋮ → **Репозитории**.
3. Вставить `https://github.com/S0ft0r/ha-dooods` и нажать «Добавить».
4. В магазине появится раздел **DOOODS** с приложением **DOOODS2**.
5. Установить, запустить, открыть страницу **faces** из бокового меню или через Ingress.

Приложение собирается локально из `Dockerfile` — на Raspberry Pi первая установка занимает несколько минут.


---

