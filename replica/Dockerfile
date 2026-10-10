FROM gradle:9.4.0-jdk21 AS build
WORKDIR /src
COPY build.gradle.kts settings.gradle.kts ./
RUN gradle dependencies --no-daemon -q || true
COPY src ./src
RUN gradle shadowJar --no-daemon -x test

FROM eclipse-temurin:21-jre
COPY --from=build /src/build/libs/app.jar /app.jar
ENV JAVA_TOOL_OPTIONS="-XX:TieredStopAtLevel=1 -XX:+UseSerialGC -Dfile.encoding=UTF-8"
CMD ["java","-jar","/app.jar"]
