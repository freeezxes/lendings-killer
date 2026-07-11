# Сервер и Деплой

## Параметры сервера
| Параметр     | Значение                                       |
|-------------|------------------------------------------------|
| IP          | `92.38.48.227`                                 |
| Домен       | `dum-e.com`                                    |
| SSH         | `ssh -i ~/.ssh/id_ed25519 deploy@92.38.48.227` |
| App dir     | `/opt/lendings/`                               |
| Systemd     | `lendings.service`                             |

## Деплой (CI/CD через GitHub Actions)
Деплой полностью автоматизирован. При пуше в ветку `main` срабатывает `.github/workflows/deploy.yml`.

```bash
# Правильный способ деплоя:
git add .
git commit -m "Your feature description"
git push origin main
```
*Всё остальное (копирование файлов на сервер, исключение папок `generated_sites` и `lendings.db`, перезапуск `sudo systemctl restart lendings`) GitHub Actions сделает сам.*

**КРИТИЧЕСКОЕ ПРАВИЛО:**
Мы работаем **только** через GitHub Actions. Запрещено использовать ручные команды `rsync` или `scp` для загрузки кода на продакшен-сервер.
