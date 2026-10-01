package ezmanga;

import android.content.SharedPreferences;
import androidx.preference.PreferenceScreen;
import com.google.gson.*;
import eu.kanade.tachiyomi.source.ConfigurableSource;
import eu.kanade.tachiyomi.source.Source;
import eu.kanade.tachiyomi.source.model.*;
import sandbox.AndroidEnvKt;
import sandbox.ExtensionPreferencesKt;

import java.util.*;

final class SourceOptions {
    private SourceOptions() {}

    private static SharedPreferences preferenceStore(ConfigurableSource source, boolean legacy) throws ReflectiveOperationException {
        if (legacy) {
            var candidates = Collections.newSetFromMap(new IdentityHashMap<SharedPreferences, Boolean>());
            for (Class<?> type = source.getClass(); type != Object.class; type = type.getSuperclass()) {
                for (String name : List.of("getPreferences", "getPrefs")) {
                    try {
                        var getter = type.getDeclaredMethod(name);
                        if (!SharedPreferences.class.isAssignableFrom(getter.getReturnType()) || !getter.trySetAccessible()) continue;
                        Object value = getter.invoke(source);
                        if (value instanceof SharedPreferences preferences) return preferences;
                    } catch (NoSuchMethodException missing) {
                        continue;
                    }
                }
                for (var field : type.getDeclaredFields()) {
                    if (!SharedPreferences.class.isAssignableFrom(field.getType()) || !field.trySetAccessible()) continue;
                    Object value = field.get(source);
                    if (value instanceof SharedPreferences preferences) {
                        if (List.of("preferences", "prefs", "sourcePreferences").contains(field.getName())) return preferences;
                        candidates.add(preferences);
                    }
                }
            }
            if (candidates.size() == 1) return candidates.iterator().next();
            if (candidates.size() > 1) throw new IllegalStateException("Legacy extension uses multiple preference stores");
        }
        return source.getSourcePreferences();
    }

    static JsonArray preferences(Source source, boolean legacy) throws ReflectiveOperationException {
        var result = new JsonArray();
        if (!(source instanceof ConfigurableSource configurable)) return result;
        var screen = new PreferenceScreen(AndroidEnvKt.getSandboxContext());
        screen.setSharedPreferences(preferenceStore(configurable, legacy));
        configurable.setupPreferenceScreen(screen);
        String schema = ExtensionPreferencesKt.preferenceScreenToJson(screen);
        // Preference values and summaries may contain credentials; expose only configuration metadata.
        var fields = List.of("key", "title", "type", "visible", "enabled", "entries", "entryValues");
        for (var item : JsonParser.parseString(schema).getAsJsonArray()) {
            var safe = new JsonObject();
            for (String field : fields) {
                if (item.getAsJsonObject().has(field)) safe.add(field, item.getAsJsonObject().get(field));
            }
            result.add(safe);
        }
        return result;
    }

    static String safeError(Source source, Throwable failure, boolean legacy) {
        String message = Objects.toString(failure.getMessage(), "");
        try {
            if (source instanceof ConfigurableSource configurable) {
                for (Object value : preferenceStore(configurable, legacy).getAll().values()) {
                    var values = value instanceof Collection<?> collection ? collection : List.of(value);
                    for (Object item : values) {
                        if (item instanceof String secret && !secret.isEmpty()) {
                            message = message.replace(secret, "[redacted]");
                        }
                    }
                }
            }
        } catch (Throwable ignored) {
            return failure.getClass().getSimpleName() + ": Extension operation failed";
        }
        // Request URLs can contain credentials outside source preferences.
        message = message.replaceAll("(?i)https?://[^\\s\"'<>]+", "[request URL]");
        return failure.getClass().getSimpleName() + ": " + message;
    }

    static void setPreferences(Source source, JsonObject values, boolean legacy) throws ReflectiveOperationException {
        if (!(source instanceof ConfigurableSource configurable)) throw new IllegalArgumentException("This source has no configurable preferences");
        var store = preferenceStore(configurable, legacy);
        var screen = new PreferenceScreen(AndroidEnvKt.getSandboxContext());
        screen.setSharedPreferences(store);
        configurable.setupPreferenceScreen(screen);
        var updates = new LinkedHashMap<String, JsonObject>();
        for (var item : values.getAsJsonArray("preferences")) {
            var preference = item.getAsJsonObject();
            updates.put(preference.get("key").getAsString(), preference);
        }
        var saved = store.getAll();
        for (var preference : screen.getPreferences()) {
            var update = updates.get(preference.getKey());
            if (update == null) continue;
            var value = update.get("value");
            Object changed = switch (update.get("type").getAsString()) {
                case "String" -> value.getAsString();
                case "Boolean" -> value.getAsBoolean();
                case "Int" -> value.getAsInt();
                case "Long" -> value.getAsLong();
                case "Float" -> value.getAsFloat();
                case "StringSet" -> {
                    var strings = new HashSet<String>();
                    for (var part : value.getAsJsonArray()) strings.add(part.getAsString());
                    yield strings;
                }
                default -> throw new IllegalArgumentException("Unsupported preference type");
            };
            if (!Objects.equals(saved.get(preference.getKey()), changed) && !preference.callChangeListener(changed)) {
                throw new IllegalArgumentException("Extension rejected preference: " + preference.getKey());
            }
        }
        // Account callbacks clear stale tokens before explicitly imported tokens are saved.
        var editor = store.edit();
        for (var item : values.getAsJsonArray("preferences")) {
            var preference = item.getAsJsonObject();
            String key = preference.get("key").getAsString();
            var value = preference.get("value");
            switch (preference.get("type").getAsString()) {
                case "String" -> editor.putString(key, value.getAsString());
                case "Boolean" -> editor.putBoolean(key, value.getAsBoolean());
                case "Int" -> editor.putInt(key, value.getAsInt());
                case "Long" -> editor.putLong(key, value.getAsLong());
                case "Float" -> editor.putFloat(key, value.getAsFloat());
                case "StringSet" -> {
                    var strings = new HashSet<String>();
                    for (var part : value.getAsJsonArray()) strings.add(part.getAsString());
                    editor.putStringSet(key, strings);
                }
                default -> throw new IllegalArgumentException("Unsupported preference type");
            }
        }
        // Async preference writes can be lost when the CLI closes and restarts the JVM.
        if (!editor.commit()) throw new IllegalStateException("Could not save source preferences");
    }

    static List<Map<String, Object>> filters(FilterList filters) {
        var result = new ArrayList<Map<String, Object>>();
        for (int index = 0; index < filters.size(); index++) result.add(describe(filters.get(index), Integer.toString(index)));
        return result;
    }

    private static Map<String, Object> describe(Filter<?> filter, String path) {
        var result = new LinkedHashMap<String, Object>();
        result.put("path", path);
        result.put("name", filter.getName());
        if (filter instanceof Filter.Group<?> group) {
            result.put("type", "group");
            var children = new ArrayList<Map<String, Object>>();
            for (int index = 0; index < group.getState().size(); index++) {
                Object child = group.getState().get(index);
                if (!(child instanceof Filter<?> nested)) throw new IllegalArgumentException("Unsupported filter group item");
                children.add(describe(nested, path + "." + index));
            }
            result.put("children", children);
        } else if (filter instanceof Filter.Select<?> select) {
            result.put("type", "select");
            result.put("values", select.getDisplayValues());
            result.put("state", select.getState());
        } else if (filter instanceof Filter.Sort sort) {
            result.put("type", "sort");
            result.put("values", sort.getValues());
            result.put("state", sort.getState() == null ? null : Map.of("index", sort.getState().getIndex(), "ascending", sort.getState().getAscending()));
        } else {
            result.put("type", filter instanceof Filter.Text ? "text"
                : filter instanceof Filter.CheckBox ? "checkbox"
                : filter instanceof Filter.TriState ? "tristate"
                : filter instanceof Filter.Header ? "header"
                : filter instanceof Filter.Separator ? "separator" : "unsupported");
            if (!(filter instanceof Filter.Header) && !(filter instanceof Filter.Separator)) result.put("state", filter.getState());
        }
        return result;
    }

    static FilterList configureFilters(FilterList filters, JsonObject states) {
        for (var entry : states.entrySet()) {
            String[] path = entry.getKey().split("\\.", -1);
            Filter<?> filter = null;
            List<?> children = filters;
            for (int index = 0; index < path.length; index++) {
                if (!path[index].matches("0|[1-9][0-9]*")) throw new IllegalArgumentException("Invalid filter path " + entry.getKey());
                int position = Integer.parseInt(path[index]);
                if (position >= children.size() || !(children.get(position) instanceof Filter<?> value)) throw new IllegalArgumentException("Unknown filter path " + entry.getKey());
                filter = value;
                if (index < path.length - 1) {
                    if (!(filter instanceof Filter.Group<?> group)) throw new IllegalArgumentException("Filter path does not point into a group");
                    children = group.getState();
                }
            }
            apply(filter, entry.getValue());
        }
        return filters;
    }

    private static int integer(JsonElement value) {
        if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber()) throw new IllegalArgumentException("Filter state must be an integer");
        return value.getAsBigDecimal().intValueExact();
    }

    private static boolean bool(JsonElement value) {
        if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isBoolean()) throw new IllegalArgumentException("Filter state must be a boolean");
        return value.getAsBoolean();
    }

    private static void apply(Filter<?> filter, JsonElement value) {
        if (filter instanceof Filter.Select<?> select) {
            int index = integer(value);
            if (index < 0 || index >= select.getValues().length) throw new IllegalArgumentException("Select filter index out of range");
            select.setState(index);
        } else if (filter instanceof Filter.Sort sort) {
            if (value.isJsonNull()) {
                sort.setState(null);
            } else {
                var state = value.getAsJsonObject();
                int index = integer(state.get("index"));
                if (index < 0 || index >= sort.getValues().length) throw new IllegalArgumentException("Sort filter index out of range");
                sort.setState(new Filter.Sort.Selection(index, bool(state.get("ascending"))));
            }
        } else if (filter instanceof Filter.TriState tri) {
            int state = integer(value);
            if (state < 0 || state > 2) throw new IllegalArgumentException("Tri-state filter must be 0, 1, or 2");
            tri.setState(state);
        } else if (filter instanceof Filter.CheckBox check) {
            check.setState(bool(value));
        } else if (filter instanceof Filter.Text text) {
            if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isString()) throw new IllegalArgumentException("Text filter state must be a string");
            text.setState(value.getAsString());
        } else {
            throw new IllegalArgumentException("This filter cannot accept a configured state");
        }
    }
}
