CXX ?= g++
JSON_INCLUDE ?= ../.deps/usr/include
CPPFLAGS += -I$(JSON_INCLUDE) -Icollector
CXXFLAGS ?= -std=c++17 -g -Wall -Wextra -Werror

.PHONY: all http clean
all: build/epoll-metrics

# Optional Drogon service. DEPS_PREFIX also supports locally unpacked Ubuntu packages.
DEPS_PREFIX ?= $(abspath ../.deps/usr)
HTTP_LIBDIR = $(DEPS_PREFIX)/lib/$(shell $(CXX) -dumpmachine)
http: build/epoll-monitor

build/web_assets.h: web/embed.py web/index.html web/app.js web/style.css
	mkdir -p build
	python3 web/embed.py $@

build/epoll-monitor: collector/collector.cxx collector/collector.h collector/event_rules.cxx collector/event_rules.h http/main.cxx http/tcp_probe.cxx http/tcp_probe.h http/experiments.cxx http/experiments.h http/events.cxx http/events.h http/diagnoses.cxx http/diagnoses.h build/web_assets.h ../server/include/ngx_snapshot_directory.h makefile
	mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -pthread -Ibuild -I$(DEPS_PREFIX)/include -I$(DEPS_PREFIX)/include/jsoncpp -I/usr/include/jsoncpp \
		collector/collector.cxx collector/event_rules.cxx http/main.cxx http/tcp_probe.cxx http/experiments.cxx http/events.cxx http/diagnoses.cxx -L$(HTTP_LIBDIR) \
		-Wl,-rpath-link,$(HTTP_LIBDIR) -Wl,--disable-new-dtags,-rpath,'$$ORIGIN/../../.deps/usr/lib/$(shell $(CXX) -dumpmachine)' \
		-ldrogon -ltrantor -ljsoncpp -o $@

build/epoll-metrics: collector/collector.cxx collector/collector.h cli.cxx ../server/include/ngx_snapshot_directory.h
	mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) collector/collector.cxx cli.cxx -o $@

clean:
	$(RM) build/epoll-metrics build/epoll-monitor build/web_assets.h
