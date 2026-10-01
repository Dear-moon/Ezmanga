package ezmanga;

import eu.kanade.tachiyomi.source.CatalogueSource;
import eu.kanade.tachiyomi.source.Source;
import eu.kanade.tachiyomi.source.model.*;
import eu.kanade.tachiyomi.source.online.HttpSource;
import eu.kanade.tachiyomi.util.chapter.ChapterRecognition;
import okhttp3.Response;
import rx.Observable;
import sandbox.ReflectKt;

import java.lang.reflect.InvocationTargetException;
import java.util.List;

final class SourceCalls {
    final boolean legacy;

    SourceCalls(String api) {
        if (!List.of("1.4", "1.5", "1.6").contains(api)) throw new IllegalArgumentException("Unsupported extension API");
        legacy = !api.equals("1.6");
    }

    MangasPage search(Source source, int page, String query, FilterList filters) {
        return legacy
            ? ((CatalogueSource) source).fetchSearchManga(page, query, filters).toBlocking().single()
            : (MangasPage) ReflectKt.callSuspendMethod(source, "getSearchManga", page, query, filters);
    }

    SMangaUpdate update(Source source, SManga manga) {
        if (!legacy) return (SMangaUpdate) ReflectKt.callSuspendMethod(source, "getMangaUpdate", manga, List.of(), true, true);
        // Legacy sources may initialize request state or metadata while fetching details.
        SManga updated = source.fetchMangaDetails(manga).toBlocking().single();
        if (updated == null) throw new IllegalStateException("Extension returned no manga details");
        updated.setUrl(manga.getUrl());
        if (updated.getTitle().isBlank()) updated.setTitle(manga.getTitle());
        if (updated.getAuthor() == null) updated.setAuthor(manga.getAuthor());
        if (updated.getDescription() == null) updated.setDescription(manga.getDescription());
        if (updated.getThumbnail_url() == null) updated.setThumbnail_url(manga.getThumbnail_url());
        return new SMangaUpdate(updated, source.fetchChapterList(updated).toBlocking().single());
    }

    List<?> pages(Source source, SChapter chapter) {
        return legacy ? source.fetchPageList(chapter).toBlocking().single()
            : (List<?>) ReflectKt.callSuspendMethod(source, "getPageList", chapter);
    }

    String imageUrl(HttpSource source, Page page) {
        return legacy ? source.fetchImageUrl(page).toBlocking().single()
            : (String) ReflectKt.callSuspendMethod(source, "getImageUrl", page);
    }

    Response image(HttpSource source, Page page) throws Exception {
        if (legacy) {
            java.lang.reflect.Method method;
            try {
                method = source.getClass().getMethod("fetchImage", Page.class);
            } catch (NoSuchMethodException missing) {
                return (Response) ReflectKt.callSuspendMethod(source, "getImage", page);
            }
            try {
                method.trySetAccessible();
                Object value = method.invoke(source, page);
                return (Response) (value instanceof Observable<?> observable ? observable.toBlocking().single() : value);
            } catch (InvocationTargetException failure) {
                Throwable cause = failure.getCause();
                if (cause instanceof Exception exception) throw exception;
                if (cause instanceof Error error) throw error;
                throw failure;
            }
        }
        return (Response) ReflectKt.callSuspendMethod(source, "getImage", page);
    }

    static Throwable failureCause(Throwable failure) {
        var seen = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<Throwable, Boolean>());
        while (seen.add(failure) && failure.getCause() != null && !seen.contains(failure.getCause())) {
            // Rx appends the emitted value as a cause; it is not the operation's failure.
            if (failure.getCause().getClass().getName().equals("rx.exceptions.OnErrorThrowable$OnNextValue")) break;
            failure = failure.getCause();
        }
        return failure;
    }

    static double chapterNumber(SManga manga, SChapter chapter) {
        float number = chapter.getChapterNumber();
        if (Float.isFinite(number) && number >= 0) return number;
        // Older extensions often leave number recognition to the reader application.
        double recognized = ChapterRecognition.INSTANCE.parseChapterNumber(manga.getTitle(), chapter.getName(), null);
        return Double.isFinite(recognized) && recognized >= 0 ? recognized : -1;
    }
}
