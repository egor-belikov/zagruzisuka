# Mihomo rule-sets (`*.mrs`)

Файлы `*.mrs` в этом каталоге **не содержимое репозитория**. Их задаёт `rule-providers` в
[`proxy/mihomo.yaml`](../mihomo.yaml): Mihomo подтягивает их с GitHub (`itdoginfo/allow-domains`
releases), кладёт в этот том и обновляет по `interval` (сейчас 12 ч).

Пустой том и отсутствие `.mrs` при первом запуске — норма: они появятся после загрузки.
Локально у разработчика может лежать кэш (например только `russia_inside_domain.mrs`) — это
артефакт после `docker compose up`, а не файлы для коммита.
