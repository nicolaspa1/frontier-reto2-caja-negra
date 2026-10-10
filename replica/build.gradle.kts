plugins {
    kotlin("jvm") version "2.4.21"
    application
    id("com.gradleup.shadow") version "9.6.1"
}

group = "cn"
version = "1.0"

repositories { mavenCentral() }

val vertxVersion = "5.1.10"

dependencies {
    implementation("io.vertx:vertx-core:$vertxVersion")
    implementation("io.vertx:vertx-web:$vertxVersion")
    testImplementation(kotlin("test"))
}

kotlin { jvmToolchain(21) }

application { mainClass.set("cn.app.MainKt") }

// The rules live in ../rules (source of truth). When that directory exists they are copied into resources before compiling.
val syncRules by tasks.registering(Copy::class) {
    val rulesDir = rootDir.resolve("../rules")
    onlyIf { rulesDir.isDirectory }
    from(rulesDir) { include("policy-*.json") }
    into(layout.projectDirectory.dir("src/main/resources"))
}
tasks.processResources { dependsOn(syncRules) }

tasks.test {
    useJUnitPlatform()
    jvmArgs("-Dfile.encoding=UTF-8")
    systemProperty("log.path", rootDir.resolve("../experiments/log.jsonl").absolutePath)
    testLogging { events("failed"); showStandardStreams = true; exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL }
}

tasks.shadowJar {
    archiveFileName.set("app.jar")
    mergeServiceFiles()
}
