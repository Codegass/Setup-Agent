ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG MAVEN_VERSION=3.9.11
ARG MAVEN_ZIP_SHA256
ARG GRADLE_VERSION=8.10
ARG GRADLE_ZIP_SHA256
ARG HTTP_REPOSITORY_URL
ARG HTTP_REPOSITORY_REF
ARG HTTP_TARGET_SHA

RUN apt-get update -qq \
    && apt-get install -y -qq \
        build-essential ca-certificates coreutils curl findutils git grep \
        openjdk-11-jdk openjdk-17-jdk procps python3 \
        unzip util-linux \
    && mkdir -p /workspace/.setup_agent /tmp/sag_jobs /opt/d0/http \
    && touch /workspace/.sag_workspace_marker

RUN curl -fsSL \
        "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/${MAVEN_VERSION}/apache-maven-${MAVEN_VERSION}-bin.zip" \
        -o "/opt/d0/http/apache-maven-${MAVEN_VERSION}-bin.zip" \
    && printf '%s  %s\n' \
        "${MAVEN_ZIP_SHA256}" \
        "/opt/d0/http/apache-maven-${MAVEN_VERSION}-bin.zip" \
        | sha256sum -c - \
    && unzip -q "/opt/d0/http/apache-maven-${MAVEN_VERSION}-bin.zip" -d /opt/d0 \
    && mv "/opt/d0/apache-maven-${MAVEN_VERSION}" /opt/d0/jdk-maven

RUN curl -fsSL \
        "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" \
        -o "/opt/d0/gradle-${GRADLE_VERSION}-bin.zip" \
    && printf '%s  %s\n' \
        "${GRADLE_ZIP_SHA256}" \
        "/opt/d0/gradle-${GRADLE_VERSION}-bin.zip" \
        | sha256sum -c - \
    && unzip -q "/opt/d0/gradle-${GRADLE_VERSION}-bin.zip" -d /opt/d0 \
    && ln -s "/opt/d0/gradle-${GRADLE_VERSION}/bin/gradle" /usr/local/bin/gradle \
    && gradle --version | grep "Gradle ${GRADLE_VERSION}"

# Pin the real httpcomponents-client checkout and warm its unmodified wrapper
# while unzip is still present. Both D0 arms copy these exact tracked bytes;
# the control/treatment mutation happens only after their containers start.
RUN git clone --depth 1 --branch "${HTTP_REPOSITORY_REF}" \
        "${HTTP_REPOSITORY_URL}" /opt/d0/httpcomponents-client \
    && test "$(git -C /opt/d0/httpcomponents-client rev-parse HEAD)" = "${HTTP_TARGET_SHA}" \
    && /opt/d0/httpcomponents-client/mvnw --version \
    && git -C /opt/d0/httpcomponents-client diff --quiet \
    && test -z "$(git -C /opt/d0/httpcomponents-client status --porcelain)"

COPY jdk-project/pom.xml /opt/d0/jdk-project/pom.xml

# The absolute Maven below is reserved for the JDK-repair fixture. Its path is
# outside ToolchainManager's apache-maven-* discovery shape, so the HTTP control
# cannot silently substitute it for a broken wrapper.
RUN java11_home="$(dirname "$(dirname "$(find /usr/lib/jvm -path '*/bin/java' \
        -type f | grep 'java-11' | head -1)")")" \
    && update-alternatives --set java "${java11_home}/bin/java" \
    && update-alternatives --set javac "${java11_home}/bin/javac" \
    && "/opt/d0/jdk-maven/bin/mvn" \
        -q -f /opt/d0/jdk-project/pom.xml dependency:go-offline \
    && cp /usr/bin/unzip /opt/d0/unzip \
    && rm /usr/bin/unzip \
    && test ! -e /usr/bin/unzip \
    && test -x /opt/d0/unzip \
    && java -version 2>&1 | grep 'version "11'

LABEL org.setup-agent.d0.prepared-version="1"

WORKDIR /workspace
CMD ["/bin/bash", "-c", "while true; do sleep 30; done"]
