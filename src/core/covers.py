"""Ссылки на обложки Яндекса."""

COVER_SIZE = "400x400"


def cover_url(template: str | None) -> str:
    """Ссылка https на обложку из шаблона с %%, пустая без обложки."""
    if not template:
        return ""
    # Станция и API Музыки отдают адрес без схемы: avatars.yandex.net/…/%%
    host_path = template.split("://")[-1]
    return "https://" + host_path.replace("%%", COVER_SIZE)
