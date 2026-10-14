# Upstream: Spring PetClinic

This directory holds a copy of Spring PetClinic. The experiment in this repository builds, tests and packages it in every run. The source code is not modified. All adaptations are command line parameters of the build and are listed below.

## Source

| Field | Value |
|---|---|
| Repository | https://github.com/spring-projects/spring-petclinic |
| Commit | `500158f732419217507c7656904b8e6aa1bcc0d6` |
| Commit date | 2026-09-29T16:37:54+01:00 (committer date) |
| Branch | `main`, the commit was the head of `main` when the copy was made on 2026-10-14 |
| License | Apache License 2.0, text in [`LICENSE.txt`](LICENSE.txt), unchanged |
| Build | Maven Wrapper 3.3.4 (`distributionType=only-script`) with Apache Maven 3.9.16, parent `spring-boot-starter-parent` 4.1.0, `java.version` 17 |

## How the copy was made

```bash
git clone https://github.com/spring-projects/spring-petclinic.git
cd spring-petclinic
git checkout 500158f732419217507c7656904b8e6aa1bcc0d6
rsync -a --exclude=/.git --exclude=/.github ./ ../mcs-pipeline/app/
```

`diff -r` between the checkout and this directory reports no difference apart from `.git/`, `.github/` and this file. The copy contains the 128 files that `git ls-files` lists at the commit outside `.github/`. The Gradle wrapper JAR has the SHA-256 that Gradle publishes for version 9.5.1 (`497c8c2a7e5031f6aa847f88104aa80a93532ec32ee17bdb8d1d2f67a194a9c7`).

## Deviations from upstream

1. `.github/` is not copied. It holds `dco.yml` and three workflows (`deploy-and-test-cluster.yml`, `gradle-build.yml`, `maven-build.yml`). GitHub Actions reads workflows only from `.github/workflows/` at the repository root, so they would never run here. They also reference actions by version tag, and this repository allows only actions pinned to a full commit SHA.
2. This file is added.

No other file differs from the commit. Source code, `pom.xml`, both build wrappers and `.gitignore` are unchanged.

The new location changes one build output without a file change. The `git-commit-id-maven-plugin` reads the enclosing Git repository, so `BOOT-INF/classes/git.properties` in the JAR describes the mcs-pipeline commit and not the upstream commit. The file also records the host name (`git.build.host`) and the Git user (`git.build.user.name`, `git.build.user.email`) of the machine that runs the build.

## Build parameters

The pipeline adapts the build only through command line parameters. The stages `build` and `test` run two Maven invocations:

```bash
# stage build
./mvnw -B -o -DskipTests -Dcyclonedx.skip=true package
# stage test
./mvnw -B -o -Dcyclonedx.skip=true -Dspring-javaformat.validate.skip=true \
  -Dcheckstyle.skip=true '-Dtest=!MySqlIntegrationTests,!PostgresIntegrationTests' test
```

The evidence for each parameter is the plugin descriptor of the pinned version, read with `./mvnw help:describe -Dplugin=<groupId>:<artifactId>:<version> -Dgoal=<goal> -Ddetail`.

### `-o`

Maven runs offline. The local repository is a pinned snapshot that the job restores before the first stage, so `build` and `test` never contact Maven Central. A missing artifact stops the build with an error.

A build with an empty local repository downloads 1,087 files (209.6 MB) from Maven Central. `dependency:go-offline` is not a substitute for the snapshot: it downloads 1,458 files, but not the Surefire provider `surefire-junit-platform` that Surefire resolves at test time, and the offline test run then fails. The POMs of `bcutil-jdk18on` and `bcpkix-jdk18on` 1.81, which the build plugins pull in through JGit and Apache SSHD, declare the version range `[1.81,1.82)`. An online build resolves the range through `maven-metadata.xml` (today to 1.81.1), so the set of plugin dependencies can change with a new release inside that range.

### `-DskipTests` (stage `build`)

Surefire 3.5.6, parameter `skipTests`, user property `skipTests`: "Set this to "true" to skip running tests, but still compile them." The stage `build` therefore compiles main and test classes and packages the JAR. The tests run in the stage `test`.

### `-Dspring-javaformat.validate.skip=true` and `-Dcheckstyle.skip=true` (stage `test`)

Both plugins are bound to the phase `validate` and have already run in the stage `build`. `spring-javaformat-maven-plugin` 0.0.47, goal `validate`, parameter `skip`, user property `spring-javaformat.validate.skip`: "Skip the execution." `maven-checkstyle-plugin` 3.6.0, goal `check`, parameter `skip`, user property `checkstyle.skip`: "Skip entire check." The compiler reports "Nothing to compile - all classes are up to date." in the stage `test`, and the JaCoCo agent is attached to the test JVM as in the upstream build.

### `-Dcyclonedx.skip=true`

The parent POM binds `cyclonedx-maven-plugin` 2.9.1 (goal `makeAggregateBom`) to the phase `generate-resources`. It writes a CycloneDX 1.6 SBOM with 106 components to `META-INF/sbom/application.cdx.json` inside the JAR and attaches it as `spring-petclinic-4.0.0-SNAPSHOT-cyclonedx.json`. SBOM generation is factor A of the experiment, so the baseline must not contain it.

The plugin descriptor of version 2.9.1 documents the parameter `skip` with the user property `cyclonedx.skip`, default `false`, since 1.1.3: "Skip CycloneDX execution." The plugin documentation lists the same entry for the goal `makeAggregateBom`.

With the parameter set, the JAR contains no SBOM. The Actuator SBOM endpoint of the application then has no application SBOM to show.

The descriptor also declares `requiresOnline` for this goal. In offline mode (`-o`), Maven skips the goal and logs `Goal makeAggregateBom requires online mode for execution but Maven is currently offline, skipping`. The explicit parameter keeps the baseline free of SBOM generation in online builds as well.

### `-Dtest=!MySqlIntegrationTests,!PostgresIntegrationTests`

Two test classes need Docker. `MySqlIntegrationTests` starts `mysql:9.7` with Testcontainers and `PostgresIntegrationTests` starts `postgres:18.4` from `docker-compose.yml`. Both images come from Docker Hub. Without Docker the classes skip themselves (`@Testcontainers(disabledWithoutDocker = true)` and `assumeTrue(DockerClientFactory.instance().isDockerAvailable())`). The runners of the experiment have Docker, so the classes would run, pull images over the network and start databases.

Surefire 3.5.6 documents for the parameter `test` (user property `test`): "The test patterns prefixed with a ! will be excluded." With the parameter, 77 tests in 19 classes run. Without it, a machine without Docker reports 79 tests in 21 classes, and the 2 tests of `MySqlIntegrationTests` are skipped. The other tests use the in-memory H2 database. Their source code contains no URL except `localhost`, which they use to reach the embedded server.

## Maven distribution

`.mvn/wrapper/maven-wrapper.properties` sets no `distributionSha256Sum`, so `mvnw` would download Maven 3.9.16 without a checksum check. The pipeline installs the distribution from `tools/versions.json` after a SHA-256 check into the directory where `mvnw` looks for an existing installation (`$MAVEN_USER_HOME/wrapper/dists/apache-maven-3.9.16/<hash of the distribution URL>`). `mvnw` then starts this installation and downloads nothing. The properties file stays unchanged.
