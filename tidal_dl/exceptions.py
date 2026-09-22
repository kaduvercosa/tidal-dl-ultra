"""Exceções customizadas do tidal-dl-ultra."""


class TidalDLException(Exception):
    """Exceção base."""


class AuthenticationError(TidalDLException):
    """Login/token inválido ou expirado (HTTP 401)."""


class ForbiddenError(TidalDLException):
    """HTTP 403: a assinatura não cobre o que foi pedido."""


class ResourceNotFoundError(TidalDLException):
    """Álbum, faixa, artista ou playlist inexistente (HTTP 404)."""


class RateLimitError(TidalDLException):
    """HTTP 429: limite de requisições."""

    def __init__(self, message: str = "Limite de requisições atingido", retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class ApiError(TidalDLException):
    """Outro erro HTTP da API."""

    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


class NonStreamable(TidalDLException):
    """A faixa existe mas não pode ser baixada (região, restrição, sem URL)."""


class PreviewOnly(NonStreamable):
    """Só a prévia (30 s) está disponível: assinatura inativa/insuficiente."""


class UnsupportedProtection(NonStreamable):
    """O stream veio protegido por criptografia, que este projeto NÃO suporta.

    O downloader tenta uma qualidade menor (sem proteção) antes de desistir.
    """


class DownloadError(TidalDLException):
    """Falha de transferência/remux/verificação."""


class InvalidQuality(TidalDLException):
    pass


class ConfigError(TidalDLException):
    """config.ini inválido."""
