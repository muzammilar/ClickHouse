# docker build -t clickhouse/fasttest .
ARG FROM_TAG=latest
FROM clickhouse/test-util:$FROM_TAG

RUN apt-get update \
    && apt-get install \
        brotli \
        clang-${LLVM_VERSION} \
        clang-tidy-${LLVM_VERSION} \
        cmake \
        expect \
        file \
        libclang-${LLVM_VERSION}-dev \
        libclang-rt-${LLVM_VERSION}-dev \
        lld-${LLVM_VERSION} \
        llvm-${LLVM_VERSION}-dev \
        lsof \
        odbcinst \
        psmisc \
        python3 \
        python3-pip \
        unixodbc \
        pv \
        jq \
        unzip \
        zstd \
        --yes --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/debconf /tmp/* \
    && groupadd --system --gid 1000 clickhouse \
    && useradd --system --gid 1000 --uid 1000 -m clickhouse
# ^ For some reason, groupadd and useradd are needed for tests with 'expect', but I don't know, why.

# Install a newer ninja directly (We need 1.11 at least to support C++ modules)
ARG NINJA_VERSION=1.12.1
RUN mkdir /tmp/ninja \
    && cd /tmp/ninja \
    && curl -L https://github.com/ninja-build/ninja/archive/refs/tags/v$NINJA_VERSION.zip -o ninja.zip \
    && unzip ninja.zip \
    && cd ninja-$NINJA_VERSION \
    && ./configure.py --bootstrap \
    && cp ninja /usr/bin/ninja \
    && cd / \
    && rm -rf /tmp/ninja

COPY requirements.txt /
RUN pip3 install --no-cache-dir -r /requirements.txt

# This symlink is required by gcc to find the lld linker
RUN ln -s /usr/bin/lld-${LLVM_VERSION} /usr/bin/ld.lld
# FIXME: workaround for "The imported target "merge-fdata" references the file" error
# https://salsa.debian.org/pkg-llvm-team/llvm-toolchain/-/commit/992e52c0b156a5ba9c6a8a54f8c4857ddd3d371d
RUN sed -i '/_IMPORT_CHECK_FILES_FOR_\(mlir-\|llvm-bolt\|merge-fdata\|MLIR\)/ {s|^|#|}' /usr/lib/llvm-${LLVM_VERSION}/lib/cmake/llvm/LLVMExports-*.cmake

ARG CCACHE_VERSION=4.10.1
RUN mkdir /tmp/ccache \
    && cd /tmp/ccache \
    && curl -L \
        -O https://github.com/ccache/ccache/releases/download/v$CCACHE_VERSION/ccache-$CCACHE_VERSION.tar.xz \
        -O https://github.com/ccache/ccache/releases/download/v$CCACHE_VERSION/ccache-$CCACHE_VERSION.tar.xz.asc \
    && gpg --recv-keys --keyserver hkps://keyserver.ubuntu.com 5A939A71A46792CF57866A51996DDA075594ADB8 \
    && gpg --verify ccache-$CCACHE_VERSION.tar.xz.asc \
    && tar xf ccache-$CCACHE_VERSION.tar.xz \
    && cd /tmp/ccache/ccache-$CCACHE_VERSION \
    && cmake -DCMAKE_INSTALL_PREFIX=/usr \
        -DCMAKE_BUILD_TYPE=None \
        -DZSTD_FROM_INTERNET=ON \
        -DREDIS_STORAGE_BACKEND=OFF \
        -Wno-dev \
        -B build \
        -S . \
    && make VERBOSE=1 -C build -j \
    && make install -C build \
    && cd / \
    && rm -rf /tmp/ccache

ARG TARGETARCH
ARG SCCACHE_VERSION=v0.10.0
ENV SCCACHE_IGNORE_SERVER_IO_ERROR=1
# sccache requires a value for the region. So by default we use The Default Region
ENV SCCACHE_REGION=us-east-1
RUN arch=${TARGETARCH:-amd64} \
  && case $arch in \
    amd64) rarch=x86_64 ;; \
    arm64) rarch=aarch64 ;; \
  esac \
  && curl -Ls "https://github.com/mozilla/sccache/releases/download/$SCCACHE_VERSION/sccache-$SCCACHE_VERSION-$rarch-unknown-linux-musl.tar.gz" | \
    tar xz -C /tmp \
  && mv "/tmp/sccache-$SCCACHE_VERSION-$rarch-unknown-linux-musl/sccache" /usr/bin \
  && rm "/tmp/sccache-$SCCACHE_VERSION-$rarch-unknown-linux-musl" -r

# Give suid to gdb to grant it attach permissions
# chmod 777 to make the container user independent
RUN chmod u+s /opt/gdb/bin/gdb \
  && mkdir -p /var/lib/clickhouse \
  && chmod 777 /var/lib/clickhouse

ENV TZ=Europe/Amsterdam
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

ENV COMMIT_SHA=''
ENV PULL_REQUEST_NUMBER=''
ENV COPY_CLICKHOUSE_BINARY_TO_OUTPUT=0
