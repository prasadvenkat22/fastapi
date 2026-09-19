# Included by every proxied location. Not a server block: nginx loads *.conf
# from conf.d, so the .inc suffix keeps this from being parsed on its own.
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host $host;
proxy_connect_timeout 10s;
proxy_buffering on;
