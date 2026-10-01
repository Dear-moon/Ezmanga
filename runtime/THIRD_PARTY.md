# Runtime dependencies

Ezmanga's stdio host is original project code under the project's MIT license.

The optional compatibility library is a pinned, unmodified release artifact:

- Project: https://github.com/576576/Suwayomi-ext-runtime
- Release: `36.0.66-alpha.36683078947`
- Artifact: `ext-runtime-36.0.66.jar`
- SHA-256: `dadec0957e5c78ca39e6717cb2e379977bbd04085af49ada8c2c1ff3a9b316a9`
- License: [MPL-2.0](https://github.com/576576/Suwayomi-ext-runtime/blob/main/LICENSE)
- Source: https://github.com/576576/Suwayomi-ext-runtime/tree/36.0.66-alpha.36683078947

The artifact includes AndroidCompat (MPL-2.0), the Mihon/Tachiyomi extension API
(Apache-2.0; Copyright 2015 Javier Tomás), AOSP public API stubs (Apache-2.0),
and its declared third-party libraries. Their licenses remain applicable. See the
upstream source and its license files for the complete notices and dependency list.

Ezmanga references the artifact on its classpath and calls its API/compatibility
initializers. It does not launch `sandbox.MainKt`, the HTTP server, or the APK
conversion pipeline. Extensions come from official Keiyoushi JAR releases and
retain their own licenses; they are cached outside the project and not committed.
