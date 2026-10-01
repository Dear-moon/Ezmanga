package ezmanga;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import eu.kanade.tachiyomi.source.Source;
import eu.kanade.tachiyomi.source.SourceFactory;
import eu.kanade.tachiyomi.source.PreferenceStores;
import eu.kanade.tachiyomi.source.model.*;
import eu.kanade.tachiyomi.source.online.HttpSource;
import okhttp3.Response;
import org.w3c.dom.Element;
import sandbox.AndroidEnvKt;
import sandbox.FilePreferences;
import sandbox.InjektSetupKt;

import javax.xml.parsers.DocumentBuilderFactory;
import java.io.*;
import java.net.URLClassLoader;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.jar.JarFile;

public final class EzmangaHost {
    private static final Gson JSON = new Gson();
    private final Map<String, Source> sources = new LinkedHashMap<>();
    private final Map<String, SManga> mangas = new HashMap<>();
    private final Map<String, List<SChapter>> chapters = new HashMap<>();
    private final Map<String, SChapter> chapterHandles = new HashMap<>();
    private final Map<String, Page> pages = new HashMap<>();
    private final Map<String, SManga> chapterMangas = new HashMap<>();
    private final SourceCalls calls;
    private Source selected;
    private int nextHandle;

    private EzmangaHost(Path jar, Path settings, String api) throws Exception {
        calls = new SourceCalls(api);
        Files.createDirectories(settings);
        PreferenceStores.INSTANCE.installFactory(key -> {
            if (!key.matches("[A-Za-z0-9_.-]+")) throw new IllegalArgumentException("Invalid preference key");
            return new FilePreferences(settings.resolve(key + ".properties"));
        });
        AndroidEnvKt.installHostVersion("1", "Ezmanga");
        InjektSetupKt.setupInjekt();
        AndroidEnvKt.startMainLooper();
        AndroidEnvKt.registerAndroidCompatConfig();
        AndroidEnvKt.installSandboxContext();
        System.setProperty("http.agent", "Mozilla/5.0 (Ezmanga extension host)");
        var loader = new URLClassLoader(new java.net.URL[]{jar.toUri().toURL()}, EzmangaHost.class.getClassLoader());
        try (var archive = new JarFile(jar.toFile())) {
            var factory = DocumentBuilderFactory.newInstance();
            factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            var entry = archive.getJarEntry("AndroidManifest.xml");
            if (entry == null) throw new IllegalArgumentException("JAR has no AndroidManifest.xml");
            try (var input = archive.getInputStream(entry)) {
                var document = factory.newDocumentBuilder().parse(input);
                String pkg = document.getDocumentElement().getAttribute("package");
                var metadata = document.getElementsByTagName("meta-data");
                for (int i = 0; i < metadata.getLength(); i++) {
                    var node = (Element) metadata.item(i);
                    if (!node.getAttribute("android:name").equals("tachiyomi.extension.class")) continue;
                    for (String name : node.getAttribute("android:value").split(";")) {
                        name = name.trim();
                        if (name.startsWith(".")) name = pkg + name;
                        var instance = loader.loadClass(name).getDeclaredConstructor().newInstance();
                        var loaded = instance instanceof SourceFactory sf ? sf.createSources() : List.of((Source) instance);
                        for (Source source : loaded) sources.put(Long.toString(source.getId()), source);
                    }
                }
            }
        }
        if (sources.isEmpty()) throw new IllegalArgumentException("JAR declares no supported manga sources");
    }

    private Map<String, Object> mangaMap(SManga manga) {
        var result = new LinkedHashMap<String, Object>();
        result.put("url", manga.getUrl());
        result.put("title", manga.getTitle());
        result.put("author", manga.getAuthor());
        result.put("description", manga.getDescription());
        result.put("cover", manga.getCoverUrl() != null ? manga.getCoverUrl() : manga.getThumbnail_url());
        return result;
    }

    private SManga manga(String url) {
        return mangas.computeIfAbsent(url, key -> {
            var value = new SMangaImpl();
            value.setUrl(key);
            value.setTitle("");
            return value;
        });
    }

    private void update(String url) {
        if (chapters.containsKey(url)) return;
        var update = calls.update(selected, manga(url));
        SManga updated = update.getManga();
        if (updated != null) {
            updated.setUrl(url);
            mangas.put(url, updated);
        }
        if (update.getChapters() == null) throw new IllegalStateException("Extension returned no chapter list");
        chapters.put(url, update.getChapters());
    }

    private Object dispatch(JsonObject request) throws Exception {
        String op = request.get("op").getAsString();
        if (op.equals("sources")) {
            return sources.values().stream().map(s -> Map.of("id", Long.toString(s.getId()), "name", s.getName(), "lang", s.getLang())).toList();
        }
        if (op.equals("select")) {
            selected = sources.get(request.get("source").getAsString());
            if (selected == null) throw new IllegalArgumentException("Unknown extension source ID");
            mangas.clear(); chapters.clear(); chapterHandles.clear(); chapterMangas.clear(); pages.clear();
            return true;
        }
        if (selected == null) throw new IllegalStateException("Select an extension source first");
        switch (op) {
            case "preferences": return SourceOptions.preferences(selected, calls.legacy);
            case "set_preferences": {
                SourceOptions.setPreferences(selected, request.getAsJsonObject("values"), calls.legacy);
                return true;
            }
            case "filters": return SourceOptions.filters(selected.getFilterList());
            case "search": {
                int page = request.get("page").getAsInt();
                var filters = SourceOptions.configureFilters(selected.getFilterList(), request.getAsJsonObject("filters"));
                var result = calls.search(selected, page, request.get("query").getAsString(), filters);
                var items = new ArrayList<Map<String, Object>>();
                for (SManga item : result.getMangas()) {
                    mangas.put(item.getUrl(), item);
                    items.add(mangaMap(item));
                }
                return Map.of("items", items, "has_more", result.getHasNextPage());
            }
            case "title": {
                String url = request.get("url").getAsString();
                update(url);
                return mangaMap(manga(url));
            }
            case "chapters": {
                String url = request.get("url").getAsString();
                update(url);
                var result = new ArrayList<Map<String, Object>>();
                for (SChapter chapter : chapters.get(url)) {
                    String handle = Integer.toString(nextHandle++);
                    chapterHandles.put(handle, chapter);
                    chapterMangas.put(handle, manga(url));
                    result.add(Map.of("handle", handle, "url", chapter.getUrl(), "name", chapter.getName(), "number", SourceCalls.chapterNumber(manga(url), chapter), "scanlator", chapter.getScanlator() == null ? "" : chapter.getScanlator()));
                }
                return result;
            }
            case "pages": {
                SChapter chapter = chapterHandles.get(request.get("chapter").getAsString());
                if (chapter == null) throw new IllegalArgumentException("Unknown chapter handle");
                // Retain the original models because extension memo fields may hold decryption keys.
                if (selected instanceof HttpSource http) {
                    http.prepareNewChapter(chapter, chapterMangas.get(request.get("chapter").getAsString()));
                }
                var values = calls.pages(selected, chapter);
                pages.clear();
                var result = new ArrayList<Map<String, Object>>();
                for (Object value : values) {
                    Page page = (Page) value;
                    String handle = Integer.toString(nextHandle++);
                    pages.put(handle, page);
                    var item = new LinkedHashMap<String, Object>();
                    item.put("handle", handle); item.put("index", page.getIndex()); item.put("image_url", page.getImageUrl());
                    result.add(item);
                }
                return result;
            }
            case "image": {
                if (!(selected instanceof HttpSource http)) throw new UnsupportedOperationException("Only HTTP image sources are supported");
                Page page = pages.get(request.get("page").getAsString());
                if (page == null) throw new IllegalArgumentException("Unknown page handle");
                if (page.getImageUrl() == null || page.getImageUrl().isEmpty()) {
                    page.setImageUrl(calls.imageUrl(http, page));
                }
                // The extension client applies its own headers, cookies, and image interceptors.
                try (Response response = calls.image(http, page)) {
                    if (!response.isSuccessful() || response.body() == null) {
                        String error = "Image HTTP " + response.code();
                        if (response.code() >= 500 || response.code() == 408 || response.code() == 429) throw new IOException(error);
                        throw new IllegalStateException(error);
                    }
                    return Map.of("data", Base64.getEncoder().encodeToString(response.body().bytes()));
                }
            }
            default: throw new IllegalArgumentException("Unknown operation: " + op);
        }
    }

    private Object invoke(JsonObject request) throws Exception {
        for (int attempt = 0; ; attempt++) {
            try {
                return dispatch(request);
            } catch (Exception failure) {
                Throwable cause = SourceCalls.failureCause(failure);
                if (!(cause instanceof IOException) || attempt == 2) throw failure;
                Thread.sleep((attempt + 1) * 1000L);
            }
        }
    }

    public static void main(String[] args) throws Exception {
        var replies = new PrintStream(new FileOutputStream(FileDescriptor.out), true, StandardCharsets.UTF_8);
        System.setErr(new PrintStream(new FileOutputStream(FileDescriptor.err), true, StandardCharsets.UTF_8));
        // Windows native stdout encoding can introduce invalid backslashes into JSON.
        System.setOut(System.err);
        int status = 1;
        try {
            var host = new EzmangaHost(Path.of(args[0]), Path.of(args[1]), args[2]);
            var input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            String line;
            while ((line = input.readLine()) != null) {
                var reply = new LinkedHashMap<String, Object>();
                try {
                    var request = JsonParser.parseString(line).getAsJsonObject();
                    reply.put("id", request.get("id").getAsLong());
                    reply.put("result", host.invoke(request));
                } catch (Throwable failure) {
                    failure = SourceCalls.failureCause(failure);
                    reply.put("error", SourceOptions.safeError(host.selected, failure, host.calls.legacy));
                }
                replies.println(JSON.toJson(reply));
                replies.flush();
            }
            status = 0;
        } catch (Throwable failure) {
            failure.printStackTrace(System.err);
        } finally {
            // OkHttp and extension workers can otherwise keep the JVM alive after stdin closes.
            System.exit(status);
        }
    }
}
