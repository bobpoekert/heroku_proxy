heroku_proxy
============

A fairly efficient (non-blocking, doesn't copy data into userspace) proxy server deployable to Heroku

If you run `python app.py` it'll bind to port 5000 (or $PORT if it's set).

See http://www.corsproxy.com for more information.
