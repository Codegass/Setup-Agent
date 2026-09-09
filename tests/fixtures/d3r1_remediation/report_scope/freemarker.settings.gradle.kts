rootProject.name = "freemarker-gae"

apply(from = rootDir.toPath().resolve("gradle").resolve("repositories.gradle.kts"))

plugins {
    id("org.gradle.toolchains.foojay-resolver-convention") version "0.7.0"
}

dependencyResolutionManagement {
    versionCatalogs {
        create("libs") {
            version("junit", "4.12")

            library("junit", "junit", "junit").versionRef("junit")
        }
    }
}

include("freemarker-test-graalvm-native")