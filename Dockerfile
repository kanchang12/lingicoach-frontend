FROM nginx:alpine
COPY . /usr/share/nginx/html
COPY public/manifest.json /usr/share/nginx/html/manifest.json
COPY public/sw.js /usr/share/nginx/html/sw.js
COPY public/icons /usr/share/nginx/html/icons
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8000
CMD ["nginx", "-g", "daemon off;"]
