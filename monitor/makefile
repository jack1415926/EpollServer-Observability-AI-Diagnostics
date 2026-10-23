CXX ?= g++
JSON_INCLUDE ?= ../.deps/usr/include
CPPFLAGS += -I$(JSON_INCLUDE) -Icollector
CXXFLAGS ?= -std=c++17 -g -Wall -Wextra -Werror

.PHONY: all clean
all: build/epoll-metrics

build/epoll-metrics: collector/collector.cxx collector/collector.h cli.cxx
	mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) collector/collector.cxx cli.cxx -o $@

clean:
	$(RM) build/epoll-metrics
