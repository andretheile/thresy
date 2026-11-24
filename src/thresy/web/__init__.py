"""The web dashboard: a read-only overview of the athlete's data in a browser, behind a login.

Modules:

* :mod:`~thresy.web.app`: the server (Starlette), the login, the headers of every response.
* :mod:`~thresy.web.security`: password hashing, signed sessions and tokens, rate limit.
* :mod:`~thresy.web.settings`: ``COACH_WEB_*``.
* :mod:`~thresy.web.source`: where the numbers come from (the database through the
  ``coach`` library functions, or :mod:`~thresy.web.demo`).
* :mod:`~thresy.web.pages`: what each page shows; :mod:`~thresy.web.charts`: inline SVG.
* :mod:`~thresy.web.render`, :mod:`~thresy.web.export`, :mod:`~thresy.web.i18n`: HTML, the
  static demo, texts in English and German.

docs/web.md describes setup, settings and the security model.
"""
