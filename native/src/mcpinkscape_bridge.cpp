// SPDX-License-Identifier: GPL-2.0-or-later
// Minimal loaded Inkscape bridge lifecycle implementation.
//
// Document-mutating requests never run on the socket thread. They are
// synchronously marshalled onto Inkscape's GUI main context.

#include <atomic>
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <cstdlib>
#include <condition_variable>
#include <cstdint>
#include <cmath>
#include <functional>
#include <memory>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <sys/stat.h>

#include <glib.h>
#include <glib/gstdio.h>
#include <gmodule.h>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#else
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/types.h>
#include <limits.h>
#include <unistd.h>
#ifdef __FreeBSD__
#include <sys/sysctl.h>
#endif
#endif

#include "extension/effect.h"
#include "extension/implementation/implementation.h"
#include "bridge_json.h"
#include "desktop.h"
#include "document.h"
#include "document-undo.h"
#include "inkscape.h"
#include "inkscape-application.h"
#include "inkscape-window.h"
#include "ui/widget/desktop-widget.h"
#include "inkscape-version.h"
#include "gc-finalized.h"
#include "layer-manager.h"
#include "object/sp-object.h"
#include "object/sp-namedview.h"
#include "object/sp-defs.h"
#include "object/sp-root.h"
#include "object/sp-text.h"
#include "object/sp-flowtext.h"
#include "style.h"
#include "text-editing.h"
#include "xml/attribute-record.h"
#include "xml/node-observer.h"
#include "xml/log-builder.h"
#include "xml/event-fns.h"
#include <stdexcept>
#include <set>
#include "selection.h"
#include "xml/document.h"
#include "xml/node.h"
#include "xml/repr.h"

namespace {

constexpr std::size_t MAX_LINE_BYTES = 1024 * 1024;

std::string json_string(std::string const &value)
{
    std::string output = "\"";
    for (auto const character : value) {
        switch (character) {
            case '\\': output += "\\\\"; break;
            case '\"': output += "\\\""; break;
            case '\n': output += "\\n"; break;
            case '\r': output += "\\r"; break;
            case '\t': output += "\\t"; break;
            default:
                if (static_cast<unsigned char>(character) < 0x20) output += "?";
                else output += character;
        }
    }
    return output + "\"";
}

std::optional<std::string> current_inkscape_executable();

std::string bridge_state_directory()
{
    if (auto const *endpoint = g_getenv("MCPINKSCAPE_BRIDGE_SOCKET")) {
        auto *parent = g_path_get_dirname(endpoint);
        std::string directory(parent);
        g_free(parent);
        return directory;
    }
    auto const *state = g_getenv("XDG_STATE_HOME");
    if (!state || !*state) state = g_get_user_state_dir();
    return std::string(state ? state : "") + "/mcpinkscape";
}

std::string managed_session_id()
{
    auto const *session = g_getenv("MCPINKSCAPE_SESSION_ID");
    return session ? session : "";
}

unsigned long application_pid()
{
#ifdef _WIN32
    return GetCurrentProcessId();
#else
    return getpid();
#endif
}

std::atomic_bool application_shutdown_pending = false;
std::atomic_bool application_quit_scheduled = false;

std::string const &native_instance_id()
{
    static std::string const identifier = [] {
        auto *value = g_uuid_string_random();
        std::string result(value);
        g_free(value);
        return result;
    }();
    return identifier;
}

struct DocumentTarget {
    std::string instance_id;
    std::string document_id;
};

thread_local std::optional<DocumentTarget> requested_target;
thread_local std::string requested_method;
thread_local SPDesktop *executing_desktop = nullptr;

SPDesktop *request_desktop() { return executing_desktop; }

// Only the GUI callback installs a batch context. Nested typed mutations reuse
// their normal validators but defer undo/revision commits to the outer batch.
thread_local SPDocument *batch_document = nullptr;
thread_local std::string batch_default_layer_id;
SPDocument *request_document() { return batch_document ? batch_document : (request_desktop() ? request_desktop()->getDocument() : nullptr); }

std::string json_value(mcpinkscape::json::Value const &value)
{
    using Type = mcpinkscape::json::Value::Type;
    switch (value.type) {
        case Type::null: return "null";
        case Type::boolean: return value.boolean ? "true" : "false";
        case Type::number: {
            char buffer[G_ASCII_DTOSTR_BUF_SIZE];
            return g_ascii_dtostr(buffer, sizeof(buffer), value.number);
        }
        case Type::string: return json_string(value.string);
        case Type::array: {
            std::string result = "[";
            for (auto const &item : value.array) { if (result.size() > 1) result += ","; result += json_value(item); }
            return result + "]";
        }
        case Type::object: {
            std::string result = "{";
            for (auto const &[key, item] : value.object) { if (result.size() > 1) result += ","; result += json_string(key) + ":" + json_value(item); }
            return result + "}";
        }
    }
    return "null";
}

std::string xml_fingerprint(Inkscape::XML::Node const *root)
{
    auto *checksum = g_checksum_new(G_CHECKSUM_SHA256);
    auto append = [checksum](std::string const &value) {
        auto const size = static_cast<std::uint64_t>(value.size());
        g_checksum_update(checksum, reinterpret_cast<guchar const *>(&size), sizeof(size));
        g_checksum_update(checksum, reinterpret_cast<guchar const *>(value.data()), value.size());
    };
    std::function<void(Inkscape::XML::Node const *)> visit = [&](auto const *node) {
        append(std::to_string(static_cast<unsigned>(node->type())));
        append(node->name() ? node->name() : "");
        append(node->content() ? node->content() : "");
        std::map<std::string, std::string> attributes;
        for (auto const &attribute : node->attributeList()) attributes.emplace(g_quark_to_string(attribute.key), attribute.value.pointer());
        append(std::to_string(attributes.size()));
        for (auto const &[name, value] : attributes) { append(name); append(value); }
        unsigned children = 0;
        for (auto const *child = node->firstChild(); child; child = child->next()) ++children;
        append(std::to_string(children));
        for (auto const *child = node->firstChild(); child; child = child->next()) visit(child);
    };
    visit(root);
    std::string result = g_checksum_get_string(checksum);
    g_checksum_free(checksum);
    return result;
}

std::string response_for_request_impl(std::string const &line);

class BatchJournal final : public Inkscape::XML::NodeObserver {
public:
    explicit BatchJournal(Inkscape::XML::Node *root) : _root(root) { _root->addSubtreeObserver(*this); }
    ~BatchJournal() override { stop(); if (_log) sp_repr_free_log(_log); }
    void stop() {
        if (_root) {
            _root->removeSubtreeObserver(*this);
            _root = nullptr;
            _log = _builder.detach();
        }
    }
    void rollback() {
        stop();
        if (_log) { sp_repr_undo_log(_log); sp_repr_free_log(_log); _log = nullptr; }
    }
    void notifyChildAdded(Inkscape::XML::Node &n, Inkscape::XML::Node &c, Inkscape::XML::Node *p) override { _builder.addChild(n,c,p); }
    void notifyChildRemoved(Inkscape::XML::Node &n, Inkscape::XML::Node &c, Inkscape::XML::Node *p) override { _builder.removeChild(n,c,p); }
    void notifyChildOrderChanged(Inkscape::XML::Node &n, Inkscape::XML::Node &c, Inkscape::XML::Node *a, Inkscape::XML::Node *b) override { _builder.setChildOrder(n,c,a,b); }
    void notifyContentChanged(Inkscape::XML::Node &n, Inkscape::Util::ptr_shared a, Inkscape::Util::ptr_shared b) override { _builder.setContent(n,a,b); }
    void notifyAttributeChanged(Inkscape::XML::Node &n, GQuark k, Inkscape::Util::ptr_shared a, Inkscape::Util::ptr_shared b) override { _builder.setAttribute(n,k,a,b); }
    void notifyElementNameChanged(Inkscape::XML::Node &n, GQuark a, GQuark b) override { _builder.setElementName(n,a,b); }
private:
    Inkscape::XML::Node *_root;
    Inkscape::XML::LogBuilder _builder;
    Inkscape::XML::Event *_log = nullptr;
};

bool document_method(std::string const &method)
{
    return (method.starts_with("document.") && method != "document.list") ||
        method.starts_with("object.") || method.starts_with("shape.") ||
        method.starts_with("text.") || method.starts_with("gradient.") ||
        method.starts_with("image.") || method.starts_with("layer.") || method.starts_with("selection.") || method == "snapshot.render";
}

void quit_after_response()
{
    if (!application_shutdown_pending.load() || application_quit_scheduled.exchange(true)) return;
    g_idle_add([](gpointer) -> gboolean {
        if (auto *application = InkscapeApplication::instance()) application->on_quit_immediate();
        return G_SOURCE_REMOVE;
    }, nullptr);
}

// Socket worker threads must never inspect or modify Inkscape document state.
// This small executor transfers one request at a time to the GTK main context
// and retains the waiting request storage until the callback has completed.
struct GradientStop {
    std::string offset;
    std::string color;
    std::string opacity;
};

class GuiExecutor final {
public:
    std::optional<std::string> apply_batch(std::vector<std::string> requests, std::uint64_t expected_revision)
    {
        return execute([this, requests = std::move(requests), expected_revision] {
            auto *desktop = request_desktop();
            auto *document = request_document();
            if (!desktop || !document) return std::string("{\"state\":\"no_document\"}");
            if (_revisions[document] != expected_revision) return "{\"state\":\"revision_conflict\",\"revision\":" + std::to_string(_revisions[document]) + "}";
            document->ensureUpToDate();
            auto *initial_layer = desktop->layerManager().currentLayer();
            std::string const layer_id = initial_layer && initial_layer->getId() ? initial_layer->getId() : "";
            auto selection_ids = [desktop] {
                std::vector<std::string> ids;
                for (auto *object : desktop->getSelection()->objects()) if (object->getId()) ids.emplace_back(object->getId());
                return ids;
            };
            auto const selected = selection_ids();
            auto const generation = _selection_generations[document];
            auto const modified = document->isModifiedSinceSave();
            auto const virgin = document->getVirgin();
            auto const autosave_modified = document->isModifiedSinceAutoSave();
            BridgeMutation mutation(*this, document);
            struct Context {
                SPDocument *previous_document = batch_document;
                std::string previous_layer = batch_default_layer_id;
                std::optional<DocumentTarget> previous_target = requested_target;
                std::string previous_method = requested_method;
                ~Context() {
                    batch_document = previous_document;
                    batch_default_layer_id = previous_layer;
                    requested_target = previous_target;
                    requested_method = previous_method;
                }
            } context;
            auto failure = [](char const *code, std::size_t index, std::string const &message) {
                return std::string("{\"error\":{\"code\":") + json_string(code) + ",\"operation_index\":" + std::to_string(index) +
                    ",\"message\":" + json_string("batch operation " + std::to_string(index) + ": " + message) + "}}";
            };
            auto apply = [](std::string const &request) {
                std::string parse_error;
                auto input = mcpinkscape::json::Parser(request).parse(parse_error);
                auto const method = input->get("method")->string;
                auto const *params = input->get("params");
                if (auto const *ids = params->get("object_ids"); ids && ids->type == mcpinkscape::json::Value::Type::array) {
                    std::set<std::string> seen;
                    std::vector<SPObject *> targets;
                    for (auto const &id : ids->array) {
                        if (id.type != mcpinkscape::json::Value::Type::string) continue;
                        if (!seen.insert(id.string).second) throw std::runtime_error("object_ids contains duplicates");
                        auto *object = request_document()->getObjectById(id.string);
                        bool const destructive = method == "object.delete" || method == "object.group" || method == "object.raise" || method == "object.lower";
                        if (object && destructive && (object == request_document()->getRoot() || !dynamic_cast<SPItem *>(object))) {
                            throw std::runtime_error("batch structural edits require drawable objects below the SVG root");
                        }
                        if (object && method == "object.delete") {
                            for (auto *prior : targets) if (prior->isAncestorOf(object) || object->isAncestorOf(prior)) {
                                throw std::runtime_error("delete targets must not overlap through ancestry");
                            }
                            targets.push_back(object);
                        }
                    }
                }
                auto wire = response_for_request_impl(request);
                std::string error;
                auto response = mcpinkscape::json::Parser(wire).parse(error);
                auto const *ok = response ? response->get("ok") : nullptr;
                if (!ok || !ok->boolean) {
                    auto const *detail = response && response->get("error") ? response->get("error")->get("message") : nullptr;
                    throw std::runtime_error(detail ? detail->string : "invalid mutation response");
                }
                auto const *result = response->get("result");
                if (!result || result->type != mcpinkscape::json::Value::Type::object || result->get("state")) throw std::runtime_error("mutation did not complete");
                auto value = *result;
                value.object.erase("revision");
                std::set<std::string> affected;
                for (auto const *name : {"changed_ids", "created_ids", "deleted_ids"}) {
                    if (auto const *ids = value.get(name)) for (auto const &id : ids->array) affected.insert(id.string);
                }
                if (auto const *ids = params->get("object_ids")) for (auto const &id : ids->array) affected.insert(id.string);
                for (auto const *name : {"object_id", "group_id", "gradient_id", "layer_id"}) {
                    if (auto const *id = params->get(name); id && id->type == mcpinkscape::json::Value::Type::string) affected.insert(id->string);
                }
                mcpinkscape::json::Value ids;
                ids.type = mcpinkscape::json::Value::Type::array;
                for (auto const &id : affected) {
                    mcpinkscape::json::Value entry;
                    entry.type = mcpinkscape::json::Value::Type::string;
                    entry.string = id;
                    ids.array.push_back(std::move(entry));
                }
                value.object["affected_ids"] = std::move(ids);
                return value;
            };
            std::size_t index = 0;
            // A detached copy exercises every operation, including references to
            // objects created earlier in this batch, before any live XML edit.
            {
                auto copy = document->copy();
                batch_document = copy.get();
                batch_default_layer_id = layer_id;
                _revisions[copy.get()] = expected_revision;
                try {
                    for (index = 0; index < requests.size(); ++index) apply(requests[index]);
                    copy->ensureUpToDate();
                } catch (std::exception const &error) {
                    _revisions.erase(copy.get());
                    batch_document = nullptr;
                    return failure("batch_validation_failed", index, error.what());
                }
                _revisions.erase(copy.get());
                batch_document = nullptr;
            }
            batch_document = document;
            batch_default_layer_id = layer_id;
            auto restore_selection_and_layer = [&] {
                std::vector<SPObject *> objects;
                for (auto const &id : selected) if (auto *object = document->getObjectById(id)) objects.push_back(object);
                desktop->getSelection()->setList(objects);
                if (layer_id.empty()) desktop->layerManager().setCurrentLayer(document->getRoot());
                else if (auto *layer = document->getObjectById(layer_id)) desktop->layerManager().setCurrentLayer(layer);
                else desktop->layerManager().setCurrentLayer(document->getRoot());
            };
            auto const original_xml = xml_fingerprint(document->getReprRoot());
            BatchJournal journal(document->getReprRoot());
            std::string results = "[";
            std::set<std::string> affected;
            try {
                for (index = 0; index < requests.size(); ++index) {
                    auto result = apply(requests[index]);
                    for (auto const *name : {"affected_ids"}) {
                        if (auto const *ids = result.get(name)) for (auto const &id : ids->array) affected.insert(id.string);
                    }
                    if (index) results += ",";
                    results += json_value(result);
                    // Local-only fault injection for the disposable GUI tests.
                    // There is no request parameter or MCP tool for enabling it.
                    auto const *fault = g_getenv("MCPINKSCAPE_TEST_BATCH_FAIL_AFTER");
                    if (fault && std::to_string(index + 1) == fault) throw std::runtime_error("injected execution failure");
                }
                document->ensureUpToDate();
                restore_selection_and_layer();
                document->ensureUpToDate();
            } catch (std::exception const &error) {
                journal.rollback();
                document->ensureUpToDate();
                restore_selection_and_layer();
                document->ensureUpToDate();
                document->setModifiedSinceSave(modified);
                if (!autosave_modified) document->setModifiedSinceAutoSaveFalse();
                document->setVirgin(virgin);
                _revisions[document] = expected_revision;
                _selection_generations[document] = generation;
                return failure("batch_execution_failed", index, std::string(error.what()) + "; changes rolled back");
            }
            bool const changed = xml_fingerprint(document->getReprRoot()) != original_xml;
            if (!changed) {
                // XML setters may record equal old/new values. Undo those
                // events without clearing the pre-existing redo stack.
                journal.rollback();
                document->ensureUpToDate();
                restore_selection_and_layer();
                document->setModifiedSinceSave(modified);
                if (!autosave_modified) document->setModifiedSinceAutoSaveFalse();
                document->setVirgin(virgin);
            } else {
                journal.stop();
            }
            // A no-op must not call done(): Inkscape clears redo even when the
            // XML transaction is empty. Actual changes commit exactly once.
            if (changed) Inkscape::DocumentUndo::done(document, "MCP edit batch", "");
            _revisions[document] = expected_revision + (changed ? 1 : 0);
            _selection_generations[document] = generation + (selection_ids() != selected ? 1 : 0);
            std::string ids = "[";
            for (auto const &id : affected) { if (ids.size() > 1) ids += ","; ids += json_string(id); }
            return "{\"changed\":" + std::string(changed ? "true" : "false") + ",\"revision\":" + std::to_string(_revisions[document]) + ",\"affected_ids\":" + ids +
                "],\"operation_results\":" + results + "],\"operation_count\":" + std::to_string(requests.size()) + "}";
        });
    }

    std::optional<std::string> document_list()
    {
        return execute([this] {
            std::string documents = "[";
            std::size_t count = 0;
            if (auto *application = InkscapeApplication::instance()) for (auto *document : application->get_documents()) {
                auto *desktop = desktop_for_document(document);
                auto const revision = current_revision(document, desktop ? desktop->getSelection() : nullptr);
                if (count++) documents += ",";
                documents += "{\"document_id\":" + json_string(_document_ids.at(document)) +
                    ",\"instance_id\":" + json_string(native_instance_id()) +
                    ",\"label\":" + json_string(document->getDocumentName() ? document->getDocumentName() : "") +
                    ",\"modified\":" + (document->isModifiedSinceSave() ? "true" : "false") +
                    ",\"desktop_available\":" + (desktop ? "true" : "false") +
                    ",\"revision\":" + std::to_string(revision) +
                    ",\"selection_generation\":" + std::to_string(_selection_generations[document]) + "}";
            }
            return "{\"instance_id\":" + json_string(native_instance_id()) + ",\"documents\":" + documents + "]}";
        });
    }

    std::optional<std::string> application_status()
    {
        return execute([] {
            auto *application = InkscapeApplication::instance();
            std::string documents = "[";
            std::size_t index = 0;
            if (application) for (auto *document : application->get_documents()) {
                if (index) documents += ",";
                documents += "{\"index\":" + std::to_string(index++) + ",\"modified\":" +
                    (document->isModifiedSinceSave() ? "true}" : "false}");
            }
            return "{\"documents\":" + documents + "],\"closing\":" +
                (application_shutdown_pending.load() ? "true}" : "false}");
        });
    }

    std::optional<std::string> application_shutdown(std::string session, std::string save_mode)
    {
        return execute([session = std::move(session), save_mode = std::move(save_mode)] {
#ifdef _WIN32
            return std::string("{\"state\":\"application_lifecycle_unsupported\"}");
#else
            if (session.empty() || session != managed_session_id()) return std::string("{\"state\":\"application_not_owned\"}");
            auto *application = InkscapeApplication::instance();
            if (!application) return std::string("{\"state\":\"application_unavailable\"}");
            auto const documents = application->get_documents();
            if (save_mode == "refuse") for (auto *document : documents) {
                if (document->isModifiedSinceSave()) return std::string("{\"state\":\"unsaved_documents\"}");
            }
            std::string saved_paths = "[";
            std::size_t saved = 0;
            if (save_mode == "save") {
                auto const *root = g_getenv("MCPINKSCAPE_DOCUMENT_ROOT");
                if (!root || !g_path_is_absolute(root)) return std::string("{\"state\":\"save_root_unavailable\"}");
                auto directory = std::string(root) + "/inkscape-sessions";
                for (auto const &path : {directory, directory + "/" + session}) {
                    if (g_mkdir(path.c_str(), 0700) != 0 && errno != EEXIST) return std::string("{\"state\":\"save_directory_failed\"}");
                    struct stat information {};
                    if (lstat(path.c_str(), &information) != 0 || !S_ISDIR(information.st_mode) ||
                        information.st_uid != geteuid() || chmod(path.c_str(), 0700) != 0) {
                        return std::string("{\"state\":\"unsafe_save_directory\"}");
                    }
                }
                directory += "/" + session;
                for (auto *document : documents) {
                    if (!document->isModifiedSinceSave()) continue;
                    document->ensureUpToDate();
                    auto *identifier = g_uuid_string_random();
                    auto filename = directory + "/" + identifier + ".svg";
                    g_free(identifier);
                    if (!sp_repr_save_rebased_file(document->getReprDoc(), filename.c_str(), nullptr,
                                                  document->getDocumentBase(), filename.c_str())) {
                        return std::string("{\"state\":\"document_save_failed\"}");
                    }
                    if (chmod(filename.c_str(), 0600) != 0) return std::string("{\"state\":\"save_permissions_failed\"}");
                    if (saved++) saved_paths += ",";
                    saved_paths += json_string(filename);
                }
            }
            // Save and freeze the UI in one GUI callback before scheduling quit.
            application_shutdown_pending.store(true);
            if (auto gtk = application->gtk_app()) for (auto *window : gtk->get_windows()) window->set_sensitive(false);
            return "{\"accepted\":true,\"saved_paths\":" + saved_paths + "]}";
#endif
        });
    }

    std::optional<std::string> document_revision()
    {
        return execute([this] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) {
                return std::string("{\"document_id\":\"active\",\"open\":false}");
            }
            auto *document = request_document();
            auto const revision = current_revision(document, desktop->getSelection());
            return std::string("{\"document_id\":\"active\",\"open\":true,\"revision\":") +
                std::to_string(revision) + "}";
        });
    }

    std::optional<std::string> object_list(std::optional<std::string> parent_id, std::size_t offset, std::size_t limit)
    {
        return execute([this, parent_id = std::move(parent_id), offset, limit] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) {
                return std::string("{\"document_id\":\"active\",\"open\":false,\"objects\":[]}");
            }
            auto *document = request_document();
            auto *parent = document->getReprRoot();
            if (parent_id) {
                auto *parent_object = document->getObjectById(*parent_id);
                parent = parent_object ? parent_object->getRepr() : nullptr;
                if (!parent) return std::string("{\"state\":\"object_not_found\"}");
            }
            std::size_t seen = 0;
            std::size_t emitted = 0;
            std::string objects = "[";
            for (auto *node = parent->firstChild(); node; node = node->next()) {
                auto const *id = node->attribute("id");
                if (!id) continue;
                if (seen++ < offset) continue;
                if (emitted == limit) break;
                if (emitted++) objects += ",";
                objects += "{\"id\":" + json_string(id) + ",\"type\":" +
                    json_string(node->name() ? node->name() : "") + "}";
            }
            objects += "]";
            return std::string("{\"document_id\":\"active\",\"open\":true,\"revision\":") +
                std::to_string(current_revision(document, desktop->getSelection())) + ",\"objects\":" + objects + "}";
        });
    }

    static std::string affine_json(Geom::Affine const &affine)
    {
        std::string result = "[";
        for (unsigned i = 0; i < 6; ++i) {
            if (i) result += ",";
            result += number_json(affine[i]);
        }
        return result + "]";
    }

    static std::string number_json(double value)
    {
        if (!std::isfinite(value)) return "null";
        char buffer[G_ASCII_DTOSTR_BUF_SIZE];
        return g_ascii_dtostr(buffer, sizeof(buffer), value);
    }

    static std::string bounds_json(Geom::OptRect const &bounds)
    {
        if (!bounds) return "null";
        return "{\"x\":" + number_json(bounds->left()) + ",\"y\":" + number_json(bounds->top()) +
            ",\"width\":" + number_json(bounds->width()) + ",\"height\":" + number_json(bounds->height()) + "}";
    }

    static std::string attributes_json(Inkscape::XML::Node const *repr)
    {
        std::string result = "{";
        bool first = true;
        for (auto const &attribute : repr->attributeList()) {
            if (!first) result += ",";
            first = false;
            result += json_string(g_quark_to_string(attribute.key)) + ":" + json_string(attribute.value.pointer());
        }
        return result + "}";
    }

    static bool is_layer(SPObject *object)
    {
        auto *repr = object ? object->getRepr() : nullptr;
        auto const *mode = repr ? repr->attribute("inkscape:groupmode") : nullptr;
        return repr && std::string(repr->name()) == "svg:g" && mode && std::string(mode) == "layer";
    }

    static std::string coordinate_json(SPDocument *document)
    {
        auto *root = document->getRoot();
        return "{\"bounds_space\":\"document_px\",\"unit\":\"px\",\"css_px_per_inch\":96,"
            "\"origin\":\"top-left\",\"x_axis\":\"right\",\"y_axis\":\"down\","
            "\"drawing_values_space\":\"destination_parent_user_space\",\"root_to_document\":" + affine_json(root->i2doc_affine()) +
            ",\"page_width_px\":" + number_json(root->width.computed) +
            ",\"page_height_px\":" + number_json(root->height.computed) + "}";
    }

    std::optional<std::string> document_info()
    {
        return execute([this] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            document->ensureUpToDate();
            auto *layer = desktop->layerManager().currentLayer();
            return "{\"revision\":" + std::to_string(_revisions[document]) + ",\"coordinates\":" + coordinate_json(document) +
                ",\"root_attributes\":" + attributes_json(document->getReprRoot()) + ",\"current_layer_id\":" +
                (layer && layer->getId() ? json_string(layer->getId()) : "null") + "}";
        });
    }

    static std::string paint_json(SPIPaint const &paint)
    {
        if (!paint.isPaintserver() && paint.isColor()) return json_string(paint.value.color.toString());
        return json_string(paint.get_value().raw());
    }

    std::optional<std::string> object_inspect(std::string object_id)
    {
        return execute([this, object_id = std::move(object_id)] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            document->ensureUpToDate();
            auto *object = document->getObjectById(object_id);
            if (!object) return std::string("{\"state\":\"object_not_found\"}");
            auto *item = dynamic_cast<SPItem *>(object);
            auto *parent = object->parent;
            auto *repr = object->getRepr();
            std::string result = "{\"revision\":" + std::to_string(_revisions[document]) +
                ",\"object\":{\"id\":" + json_string(object_id) + ",\"type\":" + json_string(repr->name()) +
                ",\"parent_id\":" + (parent && parent->getId() ? json_string(parent->getId()) : "null") +
                ",\"is_layer\":" + (is_layer(object) ? "true" : "false") + ",\"attributes\":" + attributes_json(repr);
            std::string children = "[";
            bool first = true;
            for (auto const &child : object->children) if (child.getId()) {
                if (!first) children += ",";
                first = false;
                children += json_string(child.getId());
            }
            result += ",\"child_ids\":" + children + "]";
            if (item) {
                result += ",\"local_transform\":" + affine_json(item->transform) +
                    ",\"item_to_document\":" + affine_json(item->i2doc_affine()) +
                    ",\"geometric_bounds\":" + bounds_json(item->documentGeometricBounds()) +
                    ",\"visual_bounds\":" + bounds_json(item->documentVisualBounds());
                if (dynamic_cast<SPText *>(object) || dynamic_cast<SPFlowtext *>(object)) {
                    result += ",\"text\":" + json_string(sp_te_get_string_multiline(item).raw()) +
                        ",\"text_bounds\":" + bounds_json(item->documentGeometricBounds());
                }
            } else {
                result += ",\"local_transform\":null,\"item_to_document\":null,\"geometric_bounds\":null,\"visual_bounds\":null";
            }
            if (auto *style = object->style) {
                double cumulative_opacity = 1;
                for (auto *ancestor = object; ancestor; ancestor = ancestor->parent) {
                    if (ancestor->style) cumulative_opacity *= SP_SCALE24_TO_FLOAT(ancestor->style->opacity.value);
                }
                result += ",\"effective_style\":{\"fill\":" + paint_json(style->fill) + ",\"stroke\":" + paint_json(style->stroke) +
                    ",\"fill_opacity\":" + number_json(SP_SCALE24_TO_FLOAT(style->fill_opacity.value)) +
                    ",\"stroke_opacity\":" + number_json(SP_SCALE24_TO_FLOAT(style->stroke_opacity.value)) +
                    ",\"opacity\":" + number_json(SP_SCALE24_TO_FLOAT(style->opacity.value)) +
                    ",\"cumulative_opacity\":" + number_json(cumulative_opacity) +
                    ",\"stroke_width_local\":" + number_json(style->stroke_width.computed) +
                    ",\"font_size_local\":" + number_json(style->font_size.computed) +
                    ",\"font_family\":" + json_string(style->font_family.value() ? style->font_family.value() : "") +
                    ",\"font_weight\":" + std::to_string(style->font_weight.computed) + "}";
            }
            result += "},\"coordinates\":" + coordinate_json(document) + "}";
            if (result.size() > MAX_LINE_BYTES / 2) return std::string("{\"state\":\"inspection_too_large\"}");
            return result;
        });
    }

    std::optional<std::string> layer_list(std::size_t offset, std::size_t limit)
    {
        return execute([this, offset, limit] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            document->ensureUpToDate();
            std::size_t seen = 0, emitted = 0;
            std::string result = "[";
            std::function<void(SPObject *, std::string)> visit = [&](SPObject *parent, std::string parent_layer) {
                for (auto &child : parent->children) {
                    auto next_parent = parent_layer;
                    if (is_layer(&child)) {
                        if (seen++ >= offset && emitted < limit) {
                            if (emitted++) result += ",";
                            auto *repr = child.getRepr();
                            auto const *label = repr->attribute("inkscape:label");
                            auto const *locked = repr->attribute("sodipodi:insensitive");
                            result += "{\"id\":" + (child.getId() ? json_string(child.getId()) : "null") +
                                ",\"label\":" + json_string(label ? label : "") + ",\"parent_layer_id\":" +
                                (parent_layer.empty() ? "null" : json_string(parent_layer)) +
                                ",\"locked\":" + (locked && std::string(locked) == "true" ? "true" : "false") +
                                ",\"hidden\":" + (dynamic_cast<SPItem &>(child).isHidden() ? "true" : "false") + "}";
                        }
                        if (child.getId()) next_parent = child.getId();
                    }
                    visit(&child, next_parent);
                }
            };
            visit(document->getRoot(), "");
            return "{\"revision\":" + std::to_string(_revisions[document]) + ",\"total\":" + std::to_string(seen) +
                ",\"layers\":" + result + "]}";
        });
    }

    std::optional<std::string> layer_create(std::string layer_id, std::string label, std::optional<std::string> parent_id,
                                          std::uint64_t expected_revision)
    {
        return execute([this, layer_id = std::move(layer_id), label = std::move(label), parent_id = std::move(parent_id), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) return "{\"state\":\"revision_conflict\",\"revision\":" + std::to_string(_revisions[document]) + "}";
            if (document->getObjectById(layer_id)) return std::string("{\"state\":\"duplicate_id\"}");
            SPObject *parent = document->getRoot();
            if (parent_id) {
                parent = document->getObjectById(*parent_id);
                if (!is_layer(parent)) return std::string("{\"state\":\"layer_not_found\"}");
            }
            BridgeMutation mutation(*this, document);
            auto *repr = document->getReprDoc()->createElement("svg:g");
            repr->setAttribute("id", layer_id.c_str());
            repr->setAttribute("inkscape:groupmode", "layer");
            repr->setAttribute("inkscape:label", label.c_str());
            parent->appendChildRepr(repr);
            Inkscape::GC::release(repr);
            commit_mutation(document, "MCP create layer", "");
            return "{\"revision\":" + std::to_string(finish_bridge_mutation(document)) + ",\"changed_ids\":[" + json_string(layer_id) + "]}";
        });
    }

    std::optional<std::string> layer_set_current(std::string layer_id, std::uint64_t expected_revision)
    {
        return execute([this, layer_id = std::move(layer_id), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) return "{\"state\":\"revision_conflict\",\"revision\":" + std::to_string(_revisions[document]) + "}";
            auto *layer = document->getObjectById(layer_id);
            if (!is_layer(layer)) return std::string("{\"state\":\"layer_not_found\"}");
            desktop->layerManager().setCurrentLayer(layer);
            return "{\"revision\":" + std::to_string(_revisions[document]) + ",\"current_layer_id\":" + json_string(layer_id) + "}";
        });
    }

    std::optional<std::string> selection_get()
    {
        return execute([] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) {
                return std::string("{\"document_id\":\"active\",\"open\":false,\"object_ids\":[]}");
            }
            std::string ids = "[";
            std::size_t count = 0;
            for (auto *object : desktop->getSelection()->objects()) {
                auto const *repr = object ? object->getRepr() : nullptr;
                auto const *id = repr ? repr->attribute("id") : nullptr;
                if (!id) continue;
                if (count++) ids += ",";
                ids += json_string(id);
            }
            ids += "]";
            return std::string("{\"document_id\":\"active\",\"open\":true,\"object_ids\":") + ids + "}";
        });
    }

    std::optional<std::string> selection_set(std::vector<std::string> object_ids, std::optional<std::uint64_t> expected_revision)
    {
        return execute([this, object_ids = std::move(object_ids), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) {
                return std::string("{\"document_id\":\"active\",\"open\":false,\"object_ids\":[]}");
            }
            auto *document = request_document();
            if (expected_revision && _revisions[document] != *expected_revision) {
                return std::string("{\"state\":\"revision_conflict\",\"revision\":") + std::to_string(_revisions[document]) + "}";
            }
            std::vector<SPObject *> objects;
            objects.reserve(object_ids.size());
            for (auto const &id : object_ids) {
                auto *object = document->getObjectById(id);
                if (!object) return std::string("{\"error\":\"object_not_found\",\"object_id\":") + json_string(id) + "}";
                objects.push_back(object);
            }
            desktop->getSelection()->setList(objects);
            std::string ids = "[";
            for (std::size_t index = 0; index < object_ids.size(); ++index) {
                if (index) ids += ",";
                ids += json_string(object_ids[index]);
            }
            ids += "]";
            return std::string("{\"document_id\":\"active\",\"open\":true,\"object_ids\":") + ids + "}";
        });
    }

    std::optional<std::string> poll_changes(std::uint64_t after_revision, std::uint64_t after_selection_generation)
    {
        return execute([this, after_revision, after_selection_generation] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"document_id\":\"active\",\"open\":false}");
            auto *document = request_document();
            auto const revision = current_revision(document, desktop->getSelection());
            auto const selection_generation = _selection_generations[document];
            std::string ids = "[";
            std::size_t count = 0;
            for (auto *object : desktop->getSelection()->objects()) {
                auto const *repr = object ? object->getRepr() : nullptr;
                auto const *id = repr ? repr->attribute("id") : nullptr;
                if (!id) continue;
                if (count++) ids += ",";
                ids += json_string(id);
            }
            ids += "]";
            return std::string("{\"document_id\":\"active\",\"open\":true,\"revision\":") + std::to_string(revision) +
                ",\"selection_generation\":" + std::to_string(selection_generation) +
                ",\"document_changed\":" + (revision != after_revision ? "true" : "false") +
                ",\"selection_changed\":" + (selection_generation != after_selection_generation ? "true" : "false") +
                ",\"object_ids\":" + ids + "}";
        });
    }

    std::optional<std::string> set_style(
        std::vector<std::string> object_ids,
        std::map<std::string, std::string> style,
        std::uint64_t expected_revision
    )
    {
        return execute([this, object_ids = std::move(object_ids), style = std::move(style), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) {
                return std::string("{\"state\":\"no_document\"}");
            }
            auto *document = request_document();
            auto const current_revision = _revisions[document];
            if (current_revision != expected_revision) {
                return std::string("{\"state\":\"revision_conflict\",\"revision\":") +
                    std::to_string(current_revision) + "}";
            }

            std::vector<SPObject *> objects;
            objects.reserve(object_ids.size());
            for (auto const &id : object_ids) {
                auto *object = document->getObjectById(id);
                if (!object) return std::string("{\"state\":\"object_not_found\"}");
                objects.push_back(object);
            }
            BridgeMutation mutation(*this, document);
            for (auto *object : objects) {
                auto *css = sp_repr_css_attr(object->getRepr(), "style");
                for (auto const &[name, value] : style) {
                    sp_repr_css_set_property(css, name.c_str(), value.c_str());
                }
                sp_repr_css_change(object->getRepr(), css, "style");
                sp_repr_css_attr_unref(css);
            }
            commit_mutation(document, "MCP set object style", "");
            auto const next_revision = finish_bridge_mutation(document);
            std::string ids = "[";
            for (std::size_t index = 0; index < object_ids.size(); ++index) {
                if (index) ids += ",";
                ids += json_string(object_ids[index]);
            }
            ids += "]";
            return std::string("{\"revision\":") + std::to_string(next_revision) +
                ",\"changed_ids\":" + ids + "}";
        });
    }

    std::optional<std::string> create_element(
        std::string element_name,
        std::map<std::string, std::string> attributes,
        std::string text,
        std::optional<std::string> layer_id,
        std::uint64_t expected_revision,
        std::string undo_label
    )
    {
        return execute([this, element_name = std::move(element_name), attributes = std::move(attributes),
                        text = std::move(text), layer_id = std::move(layer_id), expected_revision,
                        undo_label = std::move(undo_label)] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) {
                return std::string("{\"state\":\"revision_conflict\",\"revision\":") +
                    std::to_string(_revisions[document]) + "}";
            }
            auto const id_it = attributes.find("id");
            if (id_it == attributes.end() || document->getObjectById(id_it->second)) {
                return std::string("{\"state\":\"duplicate_id\"}");
            }
            SPObject *parent = desktop->layerManager().currentLayer();
            if (batch_document) parent = batch_default_layer_id.empty() ? document->getRoot() : document->getObjectById(batch_default_layer_id);
            if (layer_id) {
                parent = document->getObjectById(*layer_id);
                if (!parent || !parent->getRepr() || std::string(parent->getRepr()->name()) != "svg:g") {
                    return std::string("{\"state\":\"layer_not_found\"}");
                }
            }
            if (!parent) return std::string("{\"state\":\"no_target_layer\"}");
            BridgeMutation mutation(*this, document);
            auto *repr = document->getReprDoc()->createElement(("svg:" + element_name).c_str());
            for (auto const &[name, value] : attributes) repr->setAttribute(name.c_str(), value.c_str());
            if (!text.empty()) {
                auto *text_node = document->getReprDoc()->createTextNode(text.c_str());
                repr->appendChild(text_node);
                Inkscape::GC::release(text_node);
            }
            parent->appendChildRepr(repr);
            Inkscape::GC::release(repr);
            commit_mutation(document, undo_label.c_str(), "");
            auto const next_revision = finish_bridge_mutation(document);
            return std::string("{\"revision\":") + std::to_string(next_revision) +
                ",\"changed_ids\":[" + json_string(id_it->second) + "]}";
        });
    }

    std::optional<std::string> transform(
        std::vector<std::string> object_ids, std::string transform_text,
        std::uint64_t expected_revision, std::string undo_label
    )
    {
        return execute([this, object_ids = std::move(object_ids), transform_text = std::move(transform_text),
                        expected_revision, undo_label = std::move(undo_label)] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) {
                return std::string("{\"state\":\"revision_conflict\",\"revision\":") +
                    std::to_string(_revisions[document]) + "}";
            }
            std::vector<SPObject *> objects;
            objects.reserve(object_ids.size());
            for (auto const &id : object_ids) {
                auto *object = document->getObjectById(id);
                if (!object || !object->getRepr()) return std::string("{\"state\":\"object_not_found\"}");
                objects.push_back(object);
            }
            BridgeMutation mutation(*this, document);
            for (auto *object : objects) {
                auto const *existing = object->getRepr()->attribute("transform");
                auto const combined = transform_text + (existing && *existing ? " " + std::string(existing) : "");
                object->getRepr()->setAttribute("transform", combined.c_str());
            }
            commit_mutation(document, undo_label.c_str(), "");
            auto const next_revision = finish_bridge_mutation(document);
            std::string ids = "[";
            for (std::size_t index = 0; index < object_ids.size(); ++index) {
                if (index) ids += ",";
                ids += json_string(object_ids[index]);
            }
            return std::string("{\"revision\":") + std::to_string(next_revision) + ",\"changed_ids\":" + ids + "]}";
        });
    }

    std::optional<std::string> set_text(std::string object_id, std::string text, std::uint64_t expected_revision)
    {
        return execute([this, object_id = std::move(object_id), text = std::move(text), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) {
                return std::string("{\"state\":\"revision_conflict\",\"revision\":") +
                    std::to_string(_revisions[document]) + "}";
            }
            auto *object = document->getObjectById(object_id);
            auto *repr = object ? object->getRepr() : nullptr;
            if (!repr) return std::string("{\"state\":\"object_not_found\"}");
            auto const *name = repr->name();
            if (!name || std::string(name) != "svg:text" || !repr->firstChild()) {
                return std::string("{\"state\":\"not_basic_text\"}");
            }
            BridgeMutation mutation(*this, document);
            repr->firstChild()->setContent(text.c_str());
            commit_mutation(document, "MCP set text", "");
            auto const next_revision = finish_bridge_mutation(document);
            return std::string("{\"revision\":") + std::to_string(next_revision) +
                ",\"changed_ids\":[" + json_string(object_id) + "]}";
        });
    }

    std::optional<std::string> set_background(std::string color, double opacity, std::uint64_t expected_revision)
    {
        return execute([this, color = std::move(color), opacity, expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) {
                return std::string("{\"state\":\"revision_conflict\",\"revision\":") +
                    std::to_string(_revisions[document]) + "}";
            }
            auto *named_view = document->getNamedView();
            auto *repr = named_view ? named_view->getRepr() : nullptr;
            if (!repr) return std::string("{\"state\":\"no_named_view\"}");
            BridgeMutation mutation(*this, document);
            repr->setAttribute("pagecolor", color.c_str());
            auto opacity_text = std::to_string(opacity);
            repr->setAttribute("inkscape:pageopacity", opacity_text.c_str());
            commit_mutation(document, "MCP set page background", "");
            auto const next_revision = finish_bridge_mutation(document);
            return std::string("{\"revision\":") + std::to_string(next_revision) + ",\"changed_ids\":[]}";
        });
    }

    std::optional<std::string> create_gradient(
        std::string kind, std::string gradient_id, std::map<std::string, std::string> geometry,
        std::vector<GradientStop> stops, std::uint64_t expected_revision
    )
    {
        return execute([this, kind = std::move(kind), gradient_id = std::move(gradient_id),
                        geometry = std::move(geometry), stops = std::move(stops), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) {
                return std::string("{\"state\":\"revision_conflict\",\"revision\":") + std::to_string(_revisions[document]) + "}";
            }
            if (document->getObjectById(gradient_id)) return std::string("{\"state\":\"duplicate_id\"}");
            auto *defs = document->getDefs();
            if (!defs) return std::string("{\"state\":\"no_defs\"}");
            BridgeMutation mutation(*this, document);
            auto *gradient = document->getReprDoc()->createElement((kind == "linear" ? "svg:linearGradient" : "svg:radialGradient"));
            gradient->setAttribute("id", gradient_id.c_str());
            gradient->setAttribute("gradientUnits", "userSpaceOnUse");
            gradient->setAttribute("data-mcp-created", "true");
            for (auto const &[name, value] : geometry) gradient->setAttribute(name.c_str(), value.c_str());
            for (auto const &stop : stops) {
                auto *stop_repr = document->getReprDoc()->createElement("svg:stop");
                stop_repr->setAttribute("offset", stop.offset.c_str());
                stop_repr->setAttribute("stop-color", stop.color.c_str());
                stop_repr->setAttribute("stop-opacity", stop.opacity.c_str());
                gradient->appendChild(stop_repr);
                Inkscape::GC::release(stop_repr);
            }
            defs->appendChildRepr(gradient);
            Inkscape::GC::release(gradient);
            commit_mutation(document, "MCP create gradient", "");
            auto const next_revision = finish_bridge_mutation(document);
            return std::string("{\"revision\":") + std::to_string(next_revision) + ",\"changed_ids\":[" + json_string(gradient_id) + "]}";
        });
    }

    std::optional<std::string> set_gradient_stops(std::string gradient_id, std::vector<GradientStop> stops, std::uint64_t expected_revision)
    {
        return execute([this, gradient_id = std::move(gradient_id), stops = std::move(stops), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) return std::string("{\"state\":\"revision_conflict\",\"revision\":") + std::to_string(_revisions[document]) + "}";
            auto *object = document->getObjectById(gradient_id);
            auto *gradient = object ? object->getRepr() : nullptr;
            auto const *name = gradient ? gradient->name() : nullptr;
            if (!name || (std::string(name) != "svg:linearGradient" && std::string(name) != "svg:radialGradient")) return std::string("{\"state\":\"gradient_not_found\"}");
            BridgeMutation mutation(*this, document);
            for (auto *child = gradient->firstChild(); child;) {
                auto *next = child->next();
                gradient->removeChild(child);
                child = next;
            }
            for (auto const &stop : stops) {
                auto *stop_repr = document->getReprDoc()->createElement("svg:stop");
                stop_repr->setAttribute("offset", stop.offset.c_str());
                stop_repr->setAttribute("stop-color", stop.color.c_str());
                stop_repr->setAttribute("stop-opacity", stop.opacity.c_str());
                gradient->appendChild(stop_repr);
                Inkscape::GC::release(stop_repr);
            }
            commit_mutation(document, "MCP set gradient stops", "");
            auto const next_revision = finish_bridge_mutation(document);
            return std::string("{\"revision\":") + std::to_string(next_revision) + ",\"changed_ids\":[" + json_string(gradient_id) + "]}";
        });
    }

    std::optional<std::string> apply_gradient(std::vector<std::string> object_ids, std::string gradient_id, std::string target, std::uint64_t expected_revision)
    {
        return execute([this, object_ids = std::move(object_ids), gradient_id = std::move(gradient_id), target = std::move(target), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) return std::string("{\"state\":\"revision_conflict\",\"revision\":") + std::to_string(_revisions[document]) + "}";
            auto *gradient_object = document->getObjectById(gradient_id);
            auto const *gradient_name = gradient_object && gradient_object->getRepr() ? gradient_object->getRepr()->name() : nullptr;
            if (!gradient_name || (std::string(gradient_name) != "svg:linearGradient" && std::string(gradient_name) != "svg:radialGradient")) return std::string("{\"state\":\"gradient_not_found\"}");
            std::vector<SPObject *> objects;
            for (auto const &id : object_ids) {
                auto *object = document->getObjectById(id);
                if (!object || !object->getRepr()) return std::string("{\"state\":\"object_not_found\"}");
                objects.push_back(object);
            }
            BridgeMutation mutation(*this, document);
            auto const paint = "url(#" + gradient_id + ")";
            for (auto *object : objects) {
                auto *css = sp_repr_css_attr(object->getRepr(), "style");
                sp_repr_css_set_property(css, target.c_str(), paint.c_str());
                sp_repr_css_change(object->getRepr(), css, "style");
                sp_repr_css_attr_unref(css);
            }
            commit_mutation(document, "MCP apply gradient", "");
            auto const next_revision = finish_bridge_mutation(document);
            std::string ids = "[";
            for (std::size_t index = 0; index < object_ids.size(); ++index) {
                if (index) ids += ",";
                ids += json_string(object_ids[index]);
            }
            return std::string("{\"revision\":") + std::to_string(next_revision) + ",\"changed_ids\":" + ids + "]}";
        });
    }

    std::optional<std::string> delete_objects(std::vector<std::string> object_ids, std::uint64_t expected_revision)
    {
        return execute([this, object_ids = std::move(object_ids), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) return std::string("{\"state\":\"revision_conflict\",\"revision\":") + std::to_string(_revisions[document]) + "}";
            std::vector<Inkscape::XML::Node *> reprs;
            for (auto const &id : object_ids) {
                auto *object = document->getObjectById(id);
                auto *repr = object ? object->getRepr() : nullptr;
                if (!repr || !repr->parent()) return std::string("{\"state\":\"object_not_found\"}");
                reprs.push_back(repr);
            }
            BridgeMutation mutation(*this, document);
            for (auto *repr : reprs) repr->parent()->removeChild(repr);
            commit_mutation(document, "MCP delete objects", "");
            auto const next_revision = finish_bridge_mutation(document);
            std::string ids = "[";
            for (std::size_t index = 0; index < object_ids.size(); ++index) {
                if (index) ids += ",";
                ids += json_string(object_ids[index]);
            }
            return std::string("{\"revision\":") + std::to_string(next_revision) + ",\"deleted_ids\":" + ids + "]}";
        });
    }

    std::optional<std::string> duplicate_objects(std::vector<std::string> object_ids, std::uint64_t expected_revision)
    {
        return execute([this, object_ids = std::move(object_ids), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) return std::string("{\"state\":\"revision_conflict\",\"revision\":") + std::to_string(_revisions[document]) + "}";
            std::vector<Inkscape::XML::Node *> reprs;
            for (auto const &id : object_ids) {
                auto *object = document->getObjectById(id);
                auto *repr = object ? object->getRepr() : nullptr;
                if (!repr || !repr->parent()) return std::string("{\"state\":\"object_not_found\"}");
                reprs.push_back(repr);
            }
            BridgeMutation mutation(*this, document);
            std::vector<std::string> created_ids;
            for (auto *repr : reprs) {
                auto *copy = repr->duplicate(document->getReprDoc());
                auto *uuid = g_uuid_string_random();
                auto const copy_id = std::string("mcp-duplicate-") + uuid;
                g_free(uuid);
                copy->setAttribute("id", copy_id.c_str());
                repr->parent()->appendChild(copy);
                Inkscape::GC::release(copy);
                created_ids.push_back(copy_id);
            }
            commit_mutation(document, "MCP duplicate objects", "");
            auto const next_revision = finish_bridge_mutation(document);
            std::string ids = "[";
            for (std::size_t index = 0; index < created_ids.size(); ++index) {
                if (index) ids += ",";
                ids += json_string(created_ids[index]);
            }
            return std::string("{\"revision\":") + std::to_string(next_revision) + ",\"created_ids\":" + ids + "]}";
        });
    }

    std::optional<std::string> group_objects(std::vector<std::string> object_ids, std::string group_id, std::uint64_t expected_revision)
    {
        return execute([this, object_ids = std::move(object_ids), group_id = std::move(group_id), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) return std::string("{\"state\":\"revision_conflict\",\"revision\":") + std::to_string(_revisions[document]) + "}";
            if (document->getObjectById(group_id)) return std::string("{\"state\":\"duplicate_id\"}");
            std::vector<Inkscape::XML::Node *> reprs;
            Inkscape::XML::Node *parent = nullptr;
            for (auto const &id : object_ids) {
                auto *object = document->getObjectById(id);
                auto *repr = object ? object->getRepr() : nullptr;
                if (!repr || !repr->parent()) return std::string("{\"state\":\"object_not_found\"}");
                if (parent && parent != repr->parent()) return std::string("{\"state\":\"different_parents\"}");
                parent = repr->parent();
                reprs.push_back(repr);
            }
            BridgeMutation mutation(*this, document);
            auto *group = document->getReprDoc()->createElement("svg:g");
            group->setAttribute("id", group_id.c_str());
            group->setAttribute("data-mcp-created", "true");
            parent->appendChild(group);
            for (auto *repr : reprs) {
                parent->removeChild(repr);
                group->appendChild(repr);
            }
            Inkscape::GC::release(group);
            commit_mutation(document, "MCP group objects", "");
            auto const next_revision = finish_bridge_mutation(document);
            return std::string("{\"revision\":") + std::to_string(next_revision) + ",\"changed_ids\":[" + json_string(group_id) + "]}";
        });
    }

    std::optional<std::string> ungroup_objects(std::string group_id, std::uint64_t expected_revision)
    {
        return execute([this, group_id = std::move(group_id), expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) return std::string("{\"state\":\"revision_conflict\",\"revision\":") + std::to_string(_revisions[document]) + "}";
            auto *object = document->getObjectById(group_id);
            auto *group = object ? object->getRepr() : nullptr;
            auto const *name = group ? group->name() : nullptr;
            if (!name || std::string(name) != "svg:g" || !group->parent()) return std::string("{\"state\":\"group_not_found\"}");
            BridgeMutation mutation(*this, document);
            auto *parent = group->parent();
            std::vector<std::string> moved_ids;
            while (auto *child = group->firstChild()) {
                auto const *child_id = child->attribute("id");
                if (child_id) moved_ids.emplace_back(child_id);
                group->removeChild(child);
                parent->appendChild(child);
            }
            parent->removeChild(group);
            commit_mutation(document, "MCP ungroup objects", "");
            auto const next_revision = finish_bridge_mutation(document);
            std::string ids = "[";
            for (std::size_t index = 0; index < moved_ids.size(); ++index) {
                if (index) ids += ",";
                ids += json_string(moved_ids[index]);
            }
            return std::string("{\"revision\":") + std::to_string(next_revision) + ",\"changed_ids\":" + ids + "]}";
        });
    }

    std::optional<std::string> reorder_objects(std::vector<std::string> object_ids, bool to_top, std::uint64_t expected_revision)
    {
        return execute([this, object_ids = std::move(object_ids), to_top, expected_revision] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (_revisions[document] != expected_revision) return std::string("{\"state\":\"revision_conflict\",\"revision\":") + std::to_string(_revisions[document]) + "}";
            std::vector<Inkscape::XML::Node *> reprs;
            Inkscape::XML::Node *parent = nullptr;
            for (auto const &id : object_ids) {
                auto *object = document->getObjectById(id);
                auto *repr = object ? object->getRepr() : nullptr;
                if (!repr || !repr->parent()) return std::string("{\"state\":\"object_not_found\"}");
                if (parent && parent != repr->parent()) return std::string("{\"state\":\"different_parents\"}");
                parent = repr->parent();
                reprs.push_back(repr);
            }
            BridgeMutation mutation(*this, document);
            if (!to_top) std::reverse(reprs.begin(), reprs.end());
            Inkscape::XML::Node *lowest_drawable_after = nullptr;
            if (!to_top) {
                for (auto *child = parent->firstChild(); child; child = child->next()) {
                    auto const *name = child->name();
                    if (!name) break;
                    auto const node_name = std::string(name);
                    if (node_name == "svg:defs" || node_name == "sodipodi:namedview") {
                        lowest_drawable_after = child;
                        continue;
                    }
                    break;
                }
            }
            for (auto *repr : reprs) {
                parent->removeChild(repr);
                if (to_top) parent->appendChild(repr);
                else parent->addChild(repr, lowest_drawable_after);
            }
            commit_mutation(document, to_top ? "MCP raise objects" : "MCP lower objects", "");
            auto const next_revision = finish_bridge_mutation(document);
            std::string ids = "[";
            for (std::size_t index = 0; index < object_ids.size(); ++index) {
                if (index) ids += ",";
                ids += json_string(object_ids[index]);
            }
            return std::string("{\"revision\":") + std::to_string(next_revision) + ",\"changed_ids\":" + ids + "]}";
        });
    }

    std::optional<std::string> render_snapshot(std::string area)
    {
        struct SnapshotStaging {
            std::string snapshot_id;
            std::string svg_path;
            std::string png_path;
            std::uint64_t revision = 0;
            std::string document_id;
        };
        auto executable = current_inkscape_executable();
        // Prefer the process environment here.  GLib caches user-directory
        // values during application startup, while a loaded extension must
        // honour the explicitly configured bridge state root.
        if (!executable) return std::string("{\"state\":\"snapshot_unavailable\"}");
        auto const directory = bridge_state_directory() + "/snapshots";
        if (g_mkdir_with_parents(directory.c_str(), 0700) != 0) return std::string("{\"state\":\"snapshot_directory_create_failed\"}");
        if (chmod(directory.c_str(), 0700) != 0) return std::string("{\"state\":\"snapshot_directory_permissions_failed\"}");
        auto staging = std::make_shared<SnapshotStaging>();
        auto *uuid = g_uuid_string_random();
        staging->snapshot_id = std::string("native-") + uuid;
        g_free(uuid);
        staging->svg_path = directory + "/" + staging->snapshot_id + ".svg";
        staging->png_path = directory + "/" + staging->snapshot_id + ".png";

        // XML serialization must run on the Inkscape GUI thread, but waiting
        // for a child renderer there deadlocks a GApplication remote request.
        auto prepared = execute([this, staging] {
            auto *desktop = request_desktop();
            if (!desktop || !request_document()) return std::string("{\"state\":\"no_document\"}");
            auto *document = request_document();
            if (!sp_repr_save_file(document->getReprDoc(), staging->svg_path.c_str())) return std::string("{\"state\":\"snapshot_save_failed\"}");
            staging->revision = _revisions[document];
            staging->document_id = _document_ids.at(document);
            return std::string("{\"prepared\":true}");
        });
        if (!prepared) return std::nullopt;
        if (prepared->find("{\"state\":") == 0) return prepared;

        std::vector<gchar *> arguments;
#ifndef _WIN32
        // Do not pass a hand-built environment to g_spawn_sync here: on the
        // FreeBSD GLib build used by Inkscape that path fails before exec.
        // The POSIX env utility preserves the normal environment while
        // removing GUI/session endpoints. The renderer needs no display;
        // inheriting one can start desktop portals or route back to this GUI.
        arguments.push_back(g_strdup("/usr/bin/env"));
        arguments.push_back(g_strdup("-u"));
        arguments.push_back(g_strdup("DBUS_SESSION_BUS_ADDRESS"));
        arguments.push_back(g_strdup("-u"));
        arguments.push_back(g_strdup("DISPLAY"));
        arguments.push_back(g_strdup("-u"));
        arguments.push_back(g_strdup("WAYLAND_DISPLAY"));
#endif
        arguments.push_back(g_strdup(executable->c_str()));
        arguments.push_back(g_strdup(staging->svg_path.c_str()));
        arguments.push_back(g_strdup(("--export-filename=" + staging->png_path).c_str()));
        arguments.push_back(g_strdup("--export-type=png"));
        arguments.push_back(g_strdup(area == "drawing" ? "--export-area-drawing" : "--export-area-page"));
        arguments.push_back(nullptr);
        gint exit_status = -1;
        gchar *renderer_error = nullptr;
        gboolean spawned = g_spawn_sync(nullptr, arguments.data(), nullptr, G_SPAWN_SEARCH_PATH, nullptr, nullptr, nullptr, &renderer_error, &exit_status, nullptr);
        for (auto *argument : arguments) g_free(argument);
        g_remove(staging->svg_path.c_str());
        struct stat info {};
        if (!spawned || exit_status != 0 || stat(staging->png_path.c_str(), &info) != 0 || !S_ISREG(info.st_mode) || info.st_size == 0) {
            if (renderer_error && *renderer_error) g_warning("mcpInkscape bridge snapshot renderer: %s", renderer_error);
            g_free(renderer_error);
            g_remove(staging->png_path.c_str());
            if (!spawned) return std::string("{\"state\":\"snapshot_spawn_failed\"}");
            if (exit_status != 0) return std::string("{\"state\":\"snapshot_renderer_exit\"}");
            return std::string("{\"state\":\"snapshot_output_missing\"}");
        }
        g_free(renderer_error);
        chmod(staging->png_path.c_str(), 0600);
        return execute([this, staging] {
            _snapshots.emplace(staging->snapshot_id, staging->png_path);
            return std::string("{\"instance_id\":") + json_string(native_instance_id()) + ",\"document_id\":" + json_string(staging->document_id) + ",\"snapshot_id\":" + json_string(staging->snapshot_id) + ",\"staging_path\":" + json_string(staging->png_path) +
                ",\"revision\":" + std::to_string(staging->revision) + "}";
        }, false);
    }

    std::optional<std::string> release_snapshot(std::string snapshot_id)
    {
        return execute([this, snapshot_id = std::move(snapshot_id)] {
            auto const item = _snapshots.find(snapshot_id);
            if (item == _snapshots.end()) return std::string("{\"state\":\"snapshot_not_found\"}");
            g_remove(item->second.c_str());
            _snapshots.erase(item);
            return std::string("{\"released\":true}");
        });
    }

private:
    SPDesktop *desktop_for_document(SPDocument *document)
    {
        SPDesktop *desktop = nullptr;
        if (auto *application = InkscapeApplication::instance()) if (auto gtk = application->gtk_app()) {
            for (auto *window : gtk->get_windows()) {
                auto *inkscape_window = dynamic_cast<InkscapeWindow *>(window);
                if (!inkscape_window || inkscape_window->get_document() != document) continue;
                auto *candidate = inkscape_window->get_desktop();
                if (candidate && (!desktop || std::less<SPDesktop *>{}(candidate, desktop))) desktop = candidate;
            }
        }
        return desktop;
    }

    class BridgeMutation final {
    public:
        BridgeMutation(GuiExecutor &executor, SPDocument *document) : _executor(executor), _document(document)
        {
            ++_executor._bridge_mutating_documents[_document];
        }

        ~BridgeMutation()
        {
            auto found = _executor._bridge_mutating_documents.find(_document);
            if (found != _executor._bridge_mutating_documents.end() && --found->second == 0) _executor._bridge_mutating_documents.erase(found);
        }

    private:
        GuiExecutor &_executor;
        SPDocument *_document;
    };

    void commit_mutation(SPDocument *document, Glib::ustring const &label, Glib::ustring const &icon)
    {
        if (batch_document != document) Inkscape::DocumentUndo::done(document, label, icon);
    }

    std::uint64_t finish_bridge_mutation(SPDocument *document)
    {
        return batch_document == document ? _revisions[document] : ++_revisions[document];
    }

    std::uint64_t current_revision(SPDocument *document, Inkscape::Selection *selection)
    {
        observe_document(document, selection);
        return _revisions[document];
    }

    void observe_document(SPDocument *document, Inkscape::Selection *selection)
    {
        if (!_document_ids.contains(document)) {
            auto *identifier = g_uuid_string_random();
            _document_ids.emplace(document, identifier);
            g_free(identifier);
        }
        if (_document_modified_connections.find(document) == _document_modified_connections.end()) {
            _revisions.try_emplace(document, 0);
            _selection_generations.try_emplace(document, 0);
            auto prior_destroy_connection = _document_destroy_connections.find(document);
            if (prior_destroy_connection != _document_destroy_connections.end()) prior_destroy_connection->second.disconnect();
            _document_destroy_connections.insert_or_assign(document, document->connectDestroy([this, document] {
                _document_modified_connections.erase(document);
                _selection_changed_connections.erase(document);
                _revisions.erase(document);
                _selection_generations.erase(document);
                _bridge_mutating_documents.erase(document);
                _document_ids.erase(document);
            }));
            _document_modified_connections.emplace(document, document->connectModified([this, document](unsigned) {
                if (_bridge_mutating_documents.find(document) == _bridge_mutating_documents.end()) ++_revisions[document];
            }));
        }
        if (selection && _selection_changed_connections.find(document) == _selection_changed_connections.end()) {
            _selection_changed_connections.emplace(document, selection->connectChanged([this, document](Inkscape::Selection *) {
                if (batch_document != document) ++_selection_generations[document];
            }));
        }
    }

    struct Request {
        std::function<std::string()> operation;
        std::mutex mutex;
        std::condition_variable condition;
        bool complete = false;
        bool started = false;
        bool cancelled = false;
        std::string result;
    };

    static gboolean dispatch(gpointer value)
    {
        auto const request = *static_cast<std::shared_ptr<Request> *>(value);
        {
            std::lock_guard lock(request->mutex);
            if (request->cancelled) return G_SOURCE_REMOVE;
            request->started = true;
        }
        auto result = request->operation();
        {
            std::lock_guard lock(request->mutex);
            request->result = std::move(result);
            request->complete = true;
        }
        request->condition.notify_one();
        return G_SOURCE_REMOVE;
    }

    static void release_request(gpointer value)
    {
        delete static_cast<std::shared_ptr<Request> *>(value);
    }

    std::optional<std::string> execute(std::function<std::string()> operation, bool bind_document = true)
    {
        if (batch_document) {
            g_assert(g_main_context_is_owner(g_main_context_default()));
            return operation();
        }
        auto request = std::make_shared<Request>();
        auto const bind = bind_document && document_method(requested_method);
        request->operation = [this, operation = std::move(operation), target = requested_target, bind] {
            SPDesktop *desktop = SP_ACTIVE_DESKTOP;
            if (bind && target) {
                if (target->instance_id != native_instance_id()) return std::string("{\"state\":\"stale_document_target\"}");
                SPDocument *document = nullptr;
                if (auto *application = InkscapeApplication::instance()) for (auto *candidate : application->get_documents()) {
                    auto found = _document_ids.find(candidate);
                    if (found != _document_ids.end() && found->second == target->document_id) document = candidate;
                }
                if (!document) return std::string("{\"state\":\"stale_document_target\"}");
                desktop = desktop_for_document(document);
                if (!desktop) return std::string("{\"state\":\"document_not_displayed\"}");
            }
            struct DesktopScope {
                SPDesktop *previous;
                ~DesktopScope() { executing_desktop = previous; }
            } scope{executing_desktop};
            executing_desktop = desktop;
            SPDocument *document = bind && desktop ? request_document() : nullptr;
            if (document) current_revision(document, desktop->getSelection());
            auto result = operation();
            // SPDocument::setModifiedSinceSave updates SP_ACTIVE_DESKTOP's
            // title even when another document was modified. Repair that UI
            // side effect without activating a window or redirecting edits.
            if (bind && target) {
                if (auto *application = InkscapeApplication::instance()) if (auto gtk = application->gtk_app()) {
                    for (auto *window : gtk->get_windows()) {
                        auto *view = dynamic_cast<InkscapeWindow *>(window);
                        if (view && view->get_document() && view->get_desktop_widget()) {
                            view->get_desktop_widget()->updateTitle(view->get_document()->getDocumentName());
                        }
                    }
                }
            }
            if (document && !result.starts_with("{\"state\":") && !result.starts_with("{\"error\":")) {
                auto const identity = json_string(_document_ids.at(document));
                auto const old_id = std::string("\"document_id\":\"active\"");
                auto const position = result.find(old_id);
                if (position != std::string::npos) result.replace(position, old_id.size(), "\"document_id\":" + identity);
                result.insert(1, "\"instance_id\":" + json_string(native_instance_id()) + "," +
                    (position == std::string::npos ? "\"document_id\":" + identity + "," : ""));
            }
            return result;
        };
        auto *retained_request = new std::shared_ptr<Request>(request);
        g_main_context_invoke_full(
            nullptr,
            G_PRIORITY_DEFAULT,
            dispatch,
            retained_request,
            release_request
        );
        std::unique_lock lock(request->mutex);
        if (!request->condition.wait_for(lock, std::chrono::seconds(5), [&request] { return request->complete; })) {
            if (!request->started) request->cancelled = true;
            return std::nullopt;
        }
        return request->result;
    }

    // Accessed only by dispatch() on the GUI main context.
    std::unordered_map<SPDocument *, std::uint64_t> _revisions;
    std::unordered_map<SPDocument *, std::string> _document_ids;
    std::unordered_map<SPDocument *, std::uint64_t> _selection_generations;
    std::unordered_map<SPDocument *, unsigned> _bridge_mutating_documents;
    std::unordered_map<SPDocument *, sigc::connection> _document_modified_connections;
    std::unordered_map<SPDocument *, sigc::connection> _document_destroy_connections;
    std::unordered_map<SPDocument *, sigc::connection> _selection_changed_connections;
    std::unordered_map<std::string, std::string> _snapshots;
};

GuiExecutor gui_executor;

std::string error_response(std::string const &id, char const *code, char const *message)
{
    return "{\"type\":\"response\",\"id\":\"" + id + "\",\"ok\":false,\"error\":{\"code\":\"" +
        code + "\",\"message\":\"" + message + "\"}}\n";
}

bool valid_style_value(std::string const &value)
{
    if (value.empty() || value.size() > 256) return false;
    for (auto const character : value) {
        if ((static_cast<unsigned char>(character) < 0x20) || character == ';' || character == '{' ||
            character == '}' || character == '<' || character == '>') return false;
    }
    return true;
}

std::optional<std::string> current_inkscape_executable()
{
#ifdef __FreeBSD__
    char path[PATH_MAX];
    std::size_t length = sizeof(path);
    int mib[] = {CTL_KERN, KERN_PROC, KERN_PROC_PATHNAME, static_cast<int>(getpid())};
    if (sysctl(mib, 4, path, &length, nullptr, 0) == 0 && length > 1) return std::string(path);
#elif defined(__linux__)
    char path[PATH_MAX];
    auto const length = readlink("/proc/self/exe", path, sizeof(path) - 1);
    if (length > 0) { path[length] = '\0'; return std::string(path); }
#elif defined(_WIN32)
    // GLib's spawn API expects UTF-8, while Windows executable paths are UTF-16.
    // Query the running executable rather than looking up another Inkscape on PATH.
    std::vector<wchar_t> path(32768);
    auto const length = GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
    if (length > 0 && length < path.size()) {
        auto *utf8 = g_utf16_to_utf8(reinterpret_cast<gunichar2 const *>(path.data()),
                                    length, nullptr, nullptr, nullptr);
        if (utf8) {
            std::string result(utf8);
            g_free(utf8);
            return result;
        }
    }
#endif
    return std::nullopt;
}

std::optional<std::string> staged_image_data_uri(std::string const &path, std::string const &mime_type)
{
#ifdef _WIN32
    (void)path;
    (void)mime_type;
    return std::nullopt;
#else
    if (mime_type != "image/png" && mime_type != "image/jpeg") return std::nullopt;
    auto const imports = bridge_state_directory() + "/imports";
    auto const canonical_imports = g_canonicalize_filename(imports.c_str(), nullptr);
    auto const canonical_path = g_canonicalize_filename(path.c_str(), nullptr);
    if (!canonical_imports || !canonical_path) {
        g_free(canonical_imports);
        g_free(canonical_path);
        return std::nullopt;
    }
    auto const permitted_prefix = std::string(canonical_imports) + "/";
    auto const permitted = std::string(canonical_path).starts_with(permitted_prefix);
    struct stat information {};
    auto const safe_file = permitted && lstat(canonical_path, &information) == 0 && S_ISREG(information.st_mode) &&
        information.st_uid == geteuid() && !(information.st_mode & 0077) && information.st_size > 0 && information.st_size <= 25 * 1024 * 1024;
    if (!safe_file) {
        g_free(canonical_imports);
        g_free(canonical_path);
        return std::nullopt;
    }
    gchar *contents = nullptr;
    gsize length = 0;
    auto const loaded = g_file_get_contents(canonical_path, &contents, &length, nullptr);
    g_free(canonical_imports);
    g_free(canonical_path);
    if (!loaded || !contents) return std::nullopt;
    bool const valid_png = length >= 24 && std::memcmp(contents, "\x89PNG\r\n\x1a\n", 8) == 0;
    bool const valid_jpeg = length >= 4 && static_cast<unsigned char>(contents[0]) == 0xff && static_cast<unsigned char>(contents[1]) == 0xd8;
    if ((mime_type == "image/png" && !valid_png) || (mime_type == "image/jpeg" && !valid_jpeg)) {
        g_free(contents);
        return std::nullopt;
    }
    auto *encoded = g_base64_encode(reinterpret_cast<guchar const *>(contents), length);
    g_free(contents);
    if (!encoded) return std::nullopt;
    auto result = std::string("data:") + mime_type + ";base64," + encoded;
    g_free(encoded);
    return result;
#endif
}

bool valid_identifier(std::string const &value)
{
    if (value.empty() || value.size() > 128) return false;
    auto const first = static_cast<unsigned char>(value.front());
    if (!(std::isalpha(first) || value.front() == '_')) return false;
    return std::all_of(value.begin() + 1, value.end(), [] (unsigned char character) {
        return std::isalnum(character) || character == '_' || character == '-' || character == '.' || character == ':';
    });
}

std::optional<std::string> scalar_value(mcpinkscape::json::Value const *value, std::size_t max_length = 128)
{
    if (!value) return std::nullopt;
    if (value->type == mcpinkscape::json::Value::Type::number) return std::to_string(value->number);
    if (value->type == mcpinkscape::json::Value::Type::string && valid_style_value(value->string) && value->string.size() <= max_length) {
        return value->string;
    }
    return std::nullopt;
}

bool valid_svg_length(std::string const &value)
{
    if (value.empty() || value.size() > 64) return false;
    char *numeric_end = nullptr;
    auto const parsed = std::strtod(value.c_str(), &numeric_end);
    if (numeric_end == value.c_str() || !std::isfinite(parsed)) return false;
    auto const suffix = std::string(numeric_end);
    if (suffix.empty()) return true;
    return suffix == "px" || suffix == "mm" || suffix == "cm" || suffix == "in" || suffix == "pt" || suffix == "pc";
}

std::optional<std::uint64_t> expected_revision_of(mcpinkscape::json::Value const *request)
{
    auto const *expected = request->get("expected_revision");
    if (!expected || expected->type != mcpinkscape::json::Value::Type::number || expected->number < 0 ||
        std::floor(expected->number) != expected->number || expected->number > 9007199254740991.0) return std::nullopt;
    return static_cast<std::uint64_t>(expected->number);
}

std::optional<std::vector<std::string>> object_ids_of(mcpinkscape::json::Value const *params, bool allow_empty = false)
{
    auto const *object_ids = params->get("object_ids");
    if (!object_ids || object_ids->type != mcpinkscape::json::Value::Type::array || object_ids->array.size() > 1000 ||
        (!allow_empty && object_ids->array.empty())) return std::nullopt;
    std::vector<std::string> ids;
    ids.reserve(object_ids->array.size());
    for (auto const &value : object_ids->array) {
        if (value.type != mcpinkscape::json::Value::Type::string || !valid_identifier(value.string)) return std::nullopt;
        ids.push_back(value.string);
    }
    return ids;
}

std::optional<std::map<std::string, std::string>> style_of(mcpinkscape::json::Value const *style)
{
    if (!style || style->type != mcpinkscape::json::Value::Type::object || style->object.empty()) return std::nullopt;
    static std::map<std::string, std::string> const style_names = {
        {"fill", "fill"}, {"stroke", "stroke"}, {"opacity", "opacity"},
        {"fill_opacity", "fill-opacity"}, {"stroke_opacity", "stroke-opacity"},
        {"stroke_width", "stroke-width"}, {"stroke_dasharray", "stroke-dasharray"},
        {"stroke_dashoffset", "stroke-dashoffset"}, {"stroke_linecap", "stroke-linecap"},
        {"stroke_linejoin", "stroke-linejoin"}, {"stroke_miterlimit", "stroke-miterlimit"},
        {"font_family", "font-family"}, {"font_size", "font-size"}, {"font_style", "font-style"},
        {"font_weight", "font-weight"}, {"text_anchor", "text-anchor"}, {"letter_spacing", "letter-spacing"},
    };
    std::map<std::string, std::string> result;
    for (auto const &[name, value] : style->object) {
        auto const name_it = style_names.find(name);
        auto const scalar = scalar_value(&value, 256);
        if (name_it == style_names.end() || !scalar) return std::nullopt;
        result.emplace(name_it->second, *scalar);
    }
    return result;
}

std::string css_text(std::map<std::string, std::string> const &style)
{
    std::string result;
    for (auto const &[name, value] : style) {
        if (!result.empty()) result += ';';
        result += name + ":" + value;
    }
    return result;
}

std::optional<std::vector<GradientStop>> gradient_stops_of(mcpinkscape::json::Value const *stops)
{
    if (!stops || stops->type != mcpinkscape::json::Value::Type::array || stops->array.size() < 2 || stops->array.size() > 32) return std::nullopt;
    std::vector<GradientStop> result;
    double previous = -1;
    for (auto const &stop : stops->array) {
        if (stop.type != mcpinkscape::json::Value::Type::object) return std::nullopt;
        auto const *offset = stop.get("offset");
        auto const *color = stop.get("colour") ? stop.get("colour") : stop.get("color");
        auto const *opacity = stop.get("opacity");
        if (!offset || offset->type != mcpinkscape::json::Value::Type::number || offset->number < 0 || offset->number > 1 || offset->number < previous ||
            !color || color->type != mcpinkscape::json::Value::Type::string || !valid_style_value(color->string) ||
            (opacity && (opacity->type != mcpinkscape::json::Value::Type::number || opacity->number < 0 || opacity->number > 1))) return std::nullopt;
        previous = offset->number;
        result.push_back({std::to_string(offset->number), color->string, std::to_string(opacity ? opacity->number : 1.0)});
    }
    return result;
}

std::optional<std::map<std::string, std::string>> gradient_geometry_of(std::string const &kind, mcpinkscape::json::Value const *geometry)
{
    if (geometry && geometry->type != mcpinkscape::json::Value::Type::object) return std::nullopt;
    auto const &input = geometry ? geometry->object : std::map<std::string, mcpinkscape::json::Value>{};
    std::map<std::string, std::string> defaults;
    if (kind == "linear") defaults = {{"x1", "0"}, {"y1", "0"}, {"x2", "1"}, {"y2", "0"}};
    else if (kind == "radial") defaults = {{"cx", "0.5"}, {"cy", "0.5"}, {"r", "0.5"}, {"fx", "0.5"}, {"fy", "0.5"}};
    else return std::nullopt;
    for (auto const &[name, value] : input) {
        if (!defaults.contains(name)) return std::nullopt;
        auto scalar = scalar_value(&value);
        if (!scalar || !valid_svg_length(*scalar)) return std::nullopt;
        defaults[name] = *scalar;
    }
    if (kind == "radial") {
        char *end = nullptr;
        auto const radius = std::strtod(defaults["r"].c_str(), &end);
        if (end == defaults["r"].c_str() || radius <= 0) return std::nullopt;
    }
    return defaults;
}

std::string mutation_response(std::string const &id, std::optional<std::string> const &result)
{
    if (!result) return error_response(id, "gui_timeout", "Inkscape GUI did not service request within 5 seconds");
    if (result->find("{\"state\":\"revision_conflict\"") == 0) {
        auto const revision_position = result->rfind(':');
        auto const current_revision = result->substr(revision_position + 1, result->size() - revision_position - 2);
        return "{\"type\":\"response\",\"id\":\"" + id +
            "\",\"ok\":false,\"error\":{\"code\":\"revision_conflict\",\"message\":\"document changed\",\"current_revision\":" + current_revision + "}}\n";
    }
    if (result->find("{\"state\":\"no_document\"") == 0) return error_response(id, "no_document", "no active Inkscape document");
    if (result->find("{\"state\":\"object_not_found\"") == 0) return error_response(id, "object_not_found", "object target does not exist");
    if (result->find("{\"state\":\"layer_not_found\"") == 0) return error_response(id, "layer_not_found", "target layer does not exist");
    if (result->starts_with("{\"state\":\"inspection_too_large\"")) return error_response(id, "inspection_too_large", "inspection exceeds 512 KiB; inspect smaller objects");
    if (result->find("{\"state\":\"duplicate_id\"") == 0) return error_response(id, "duplicate_id", "object ID already exists");
    if (result->find("{\"state\":\"no_target_layer\"") == 0) return error_response(id, "no_target_layer", "there is no active target layer");
    return "{\"type\":\"response\",\"id\":\"" + id + "\",\"ok\":true,\"result\":" + *result + "}\n";
}

std::string response_for_request_impl(std::string const &line)
{
    requested_target.reset();
    requested_method.clear();
    std::string error;
    auto request = mcpinkscape::json::Parser(line).parse(error);
    if (!request || (request->type != mcpinkscape::json::Value::Type::object)) {
        return error_response("", "invalid_request", "request must be a JSON object");
    }
    auto const *id = request->get("id");
    if (!id || (id->type != mcpinkscape::json::Value::Type::string) ||
        (id->string.empty()) || (id->string.size() > 128)) {
        return error_response("", "invalid_request", "request id is required");
    }
    if (!std::all_of(id->string.begin(), id->string.end(), [] (unsigned char value) {
        return std::isalnum(value) || value == '_' || value == '-';
    })) {
        return error_response("", "invalid_request", "request id has invalid characters");
    }
    auto const *type = request->get("type");
    auto const *version = request->get("protocol_version");
    auto const *method = request->get("method");
    auto const *params = request->get("params");
    if (!type || (type->type != mcpinkscape::json::Value::Type::string) || (type->string != "request") ||
        !version || (version->type != mcpinkscape::json::Value::Type::number) || (version->number != 1) ||
        !method || (method->type != mcpinkscape::json::Value::Type::string) ||
        !params || (params->type != mcpinkscape::json::Value::Type::object)) {
        return error_response(id->string, "invalid_request", "invalid bridge request envelope");
    }
    requested_method = method->string;
    if (auto const *target = request->get("target")) {
        auto const *instance = target->get("instance_id");
        auto const *document = target->get("document_id");
        if (target->type != mcpinkscape::json::Value::Type::object || !instance || !document ||
            instance->type != mcpinkscape::json::Value::Type::string || document->type != mcpinkscape::json::Value::Type::string ||
            !g_uuid_string_is_valid(instance->string.c_str()) || !g_uuid_string_is_valid(document->string.c_str()) ||
            !document_method(requested_method)) {
            return error_response(id->string, "invalid_request", "target requires instance_id/document_id UUIDs on a document operation");
        }
        requested_target = DocumentTarget{instance->string, document->string};
    }
    if (method->string == "bridge.hello" || method->string == "bridge.status" || method->string == "bridge.ping") {
        return "{\"type\":\"response\",\"id\":\"" + id->string +
            "\",\"ok\":true,\"result\":{\"bridge\":\"mcpinkscape\",\"protocol_version\":1,"
            "\"document_targeting\":true,\"instance_id\":" + json_string(native_instance_id()) + ","
            "\"pid\":" + std::to_string(application_pid()) + ",\"managed_session_id\":" + json_string(managed_session_id()) + ","
            "\"inkscape_version\":\"" + std::string(Inkscape::version_string) + "\","
            "\"running\":true,\"methods\":[\"document.batch\",\"document.info\",\"object.inspect\",\"layer.list\",\"layer.create\",\"layer.set_current\",\"document.list\",\"application.status\",\"application.shutdown\",\"bridge.hello\",\"bridge.status\",\"bridge.ping\",\"document.revision\",\"document.poll_changes\",\"document.set_background\",\"object.list\",\"selection.get\",\"selection.set\",\"object.set_style\",\"object.delete\",\"object.duplicate\",\"object.group\",\"object.ungroup\",\"object.raise\",\"object.lower\",\"shape.create\",\"text.create\",\"text.set\",\"gradient.create\",\"gradient.set_stops\",\"gradient.apply\",\"image.import\",\"object.move\",\"object.rotate\",\"object.scale\",\"snapshot.render\",\"snapshot.release\"]}}\n";
    }
    if (application_shutdown_pending.load()) return error_response(id->string, "application_closing", "application shutdown has been accepted");
    if (method->string == "application.status") {
        auto result = gui_executor.application_status();
        if (!result) return error_response(id->string, "gui_timeout", "GUI did not service application status");
        return "{\"type\":\"response\",\"id\":" + json_string(id->string) + ",\"ok\":true,\"result\":" + *result + "}\n";
    }
    if (method->string == "application.shutdown") {
        auto const *session = params->get("session_id");
        auto const *mode = params->get("save_mode");
        if (!session || session->type != mcpinkscape::json::Value::Type::string ||
            !mode || mode->type != mcpinkscape::json::Value::Type::string ||
            (mode->string != "save" && mode->string != "refuse" && mode->string != "discard")) {
            return error_response(id->string, "invalid_request", "shutdown requires session_id and save_mode save, refuse, or discard");
        }
        auto result = gui_executor.application_shutdown(session->string, mode->string);
        if (!result) return error_response(id->string, "gui_timeout", "GUI did not service application shutdown");
        return "{\"type\":\"response\",\"id\":" + json_string(id->string) + ",\"ok\":true,\"result\":" + *result + "}\n";
    }
    if (method->string == "document.list") {
        auto const result = gui_executor.document_list();
        if (!result) return error_response(id->string, "gui_timeout", "GUI did not service document enumeration");
        return "{\"type\":\"response\",\"id\":" + json_string(id->string) + ",\"ok\":true,\"result\":" + *result + "}\n";
    }
    if (method->string == "document.revision") {
        auto const result = gui_executor.document_revision();
        if (!result) return error_response(id->string, "gui_timeout", "Inkscape GUI did not service request within 5 seconds");
        return "{\"type\":\"response\",\"id\":\"" + id->string +
            "\",\"ok\":true,\"result\":" + *result + "}\n";
    }
    if (method->string == "document.poll_changes") {
        auto const *after_revision = params->get("after_revision");
        auto const *after_selection_generation = params->get("after_selection_generation");
        if (!after_revision || after_revision->type != mcpinkscape::json::Value::Type::number || after_revision->number < 0 ||
            !after_selection_generation || after_selection_generation->type != mcpinkscape::json::Value::Type::number || after_selection_generation->number < 0 ||
            std::floor(after_revision->number) != after_revision->number || std::floor(after_selection_generation->number) != after_selection_generation->number) {
            return error_response(id->string, "invalid_request", "document.poll_changes requires non-negative integer revisions");
        }
        auto const result = gui_executor.poll_changes(static_cast<std::uint64_t>(after_revision->number), static_cast<std::uint64_t>(after_selection_generation->number));
        if (!result) return error_response(id->string, "gui_timeout", "Inkscape GUI did not service request within 5 seconds");
        return "{\"type\":\"response\",\"id\":\"" + id->string + "\",\"ok\":true,\"result\":" + *result + "}\n";
    }
    if (method->string == "document.batch") {
        auto const *operations = params->get("operations");
        auto expected_revision = expected_revision_of(&*request);
        if (!requested_target || !expected_revision || !operations || operations->type != mcpinkscape::json::Value::Type::array ||
            operations->array.empty() || operations->array.size() > 100 || line.size() > 256 * 1024 || params->object.size() != 1) {
            return error_response(id->string, "invalid_request", "batch requires one explicit target, expected_revision, and 1 through 100 typed operations within 256 KiB");
        }
        static std::map<std::string, std::set<std::string>> const fields = {
            {"shape.create", {"kind","values","object_id","layer_id","style"}},
            {"text.create", {"text","x","y","object_id","layer_id","style"}},
            {"text.set", {"object_id","text"}},
            {"object.set_style", {"object_ids","style"}},
            {"object.move", {"object_ids","dx","dy"}},
            {"object.rotate", {"object_ids","degrees"}},
            {"object.scale", {"object_ids","scale_x","scale_y"}},
            {"object.delete", {"object_ids"}},
            {"object.group", {"object_ids","group_id"}},
            {"object.ungroup", {"group_id"}},
            {"object.raise", {"object_ids"}},
            {"object.lower", {"object_ids"}},
            {"layer.create", {"layer_id","label","parent_layer_id"}},
            {"gradient.create", {"kind","stops","geometry","gradient_id"}},
            {"gradient.set_stops", {"gradient_id","stops"}},
            {"gradient.apply", {"object_ids","gradient_id","target"}},
            {"document.set_background", {"colour","opacity"}},
        };
        std::vector<std::string> requests;
        for (auto const &operation : operations->array) {
            auto const *name = operation.get("method");
            auto const *arguments = operation.get("params");
            if (operation.object.size() != 2 || !name || name->type != mcpinkscape::json::Value::Type::string ||
                !arguments || arguments->type != mcpinkscape::json::Value::Type::object || !fields.contains(name->string)) {
                return error_response(id->string, "invalid_request", "batch contains an unsupported or malformed operation");
            }
            for (auto const &[key, value] : arguments->object) if (!fields.at(name->string).contains(key)) {
                return error_response(id->string, "invalid_request", "batch operation contains an unknown parameter");
            }
            requests.push_back("{\"type\":\"request\",\"protocol_version\":1,\"id\":\"batch-item\",\"method\":" + json_string(name->string) +
                ",\"params\":" + json_value(*arguments) + ",\"expected_revision\":" + std::to_string(*expected_revision) + "}");
        }
        auto result = gui_executor.apply_batch(std::move(requests), *expected_revision);
        if (result && result->starts_with("{\"error\":")) {
            std::string parse_error;
            auto error = mcpinkscape::json::Parser(*result).parse(parse_error);
            return "{\"type\":\"response\",\"id\":" + json_string(id->string) + ",\"ok\":false,\"error\":" + json_value(*error->get("error")) + "}\n";
        }
        return mutation_response(id->string, result);
    }
    if (method->string == "document.info") return mutation_response(id->string, gui_executor.document_info());
    if (method->string == "object.inspect") {
        auto const *object_id = params->get("object_id");
        if (!object_id || object_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(object_id->string)) {
            return error_response(id->string, "invalid_request", "object.inspect requires a valid object_id");
        }
        return mutation_response(id->string, gui_executor.object_inspect(object_id->string));
    }
    if (method->string == "layer.list") {
        auto const *offset = params->get("offset");
        auto const *limit = params->get("limit");
        if (!offset || !limit || offset->type != mcpinkscape::json::Value::Type::number ||
            limit->type != mcpinkscape::json::Value::Type::number || offset->number < 0 ||
            limit->number < 1 || limit->number > 1000 || std::floor(offset->number) != offset->number ||
            std::floor(limit->number) != limit->number || offset->number > 1000000000) {
            return error_response(id->string, "invalid_request", "layer.list requires integer offset and limit 1 through 1000");
        }
        return mutation_response(id->string, gui_executor.layer_list(offset->number, limit->number));
    }
    if (method->string == "layer.create" || method->string == "layer.set_current") {
        auto const *layer_id = params->get("layer_id");
        auto expected_revision = expected_revision_of(&*request);
        if (!layer_id || layer_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(layer_id->string) || !expected_revision) {
            return error_response(id->string, "invalid_request", "layer operation requires layer_id and non-negative integer expected_revision");
        }
        if (method->string == "layer.set_current") return mutation_response(id->string, gui_executor.layer_set_current(layer_id->string, *expected_revision));
        auto const *label = params->get("label");
        if (!label || label->type != mcpinkscape::json::Value::Type::string || label->string.empty() || label->string.size() > 1024 || label->string.find('\0') != std::string::npos) {
            return error_response(id->string, "invalid_request", "layer label must contain 1 through 1024 UTF-8 bytes without NUL");
        }
        std::optional<std::string> parent_id;
        if (auto const *parent = params->get("parent_layer_id")) {
            if (parent->type != mcpinkscape::json::Value::Type::string || !valid_identifier(parent->string)) return error_response(id->string, "invalid_request", "parent_layer_id is invalid");
            parent_id = parent->string;
        }
        return mutation_response(id->string, gui_executor.layer_create(layer_id->string, label->string, parent_id, *expected_revision));
    }
    if (method->string == "object.list") {
        auto const *offset = params->get("offset");
        auto const *limit = params->get("limit");
        if (!offset || !limit || offset->type != mcpinkscape::json::Value::Type::number ||
            limit->type != mcpinkscape::json::Value::Type::number || offset->number < 0 ||
            limit->number < 1 || limit->number > 1000 || std::floor(offset->number) != offset->number ||
            std::floor(limit->number) != limit->number) {
            return error_response(id->string, "invalid_request", "object.list requires integer offset and limit 1 through 1000");
        }
        std::optional<std::string> parent_id;
        if (auto const *parent = params->get("parent_id")) {
            if (parent->type != mcpinkscape::json::Value::Type::string || !valid_identifier(parent->string)) {
                return error_response(id->string, "invalid_request", "object.list parent_id is invalid");
            }
            parent_id = parent->string;
        }
        auto const result = gui_executor.object_list(std::move(parent_id), static_cast<std::size_t>(offset->number), static_cast<std::size_t>(limit->number));
        if (!result) return error_response(id->string, "gui_timeout", "Inkscape GUI did not service request within 5 seconds");
        if (result->find("{\"state\":\"object_not_found\"") == 0) return error_response(id->string, "object_not_found", "object.list parent does not exist");
        return "{\"type\":\"response\",\"id\":\"" + id->string +
            "\",\"ok\":true,\"result\":" + *result + "}\n";
    }
    if (method->string == "selection.get") {
        auto const result = gui_executor.selection_get();
        if (!result) return error_response(id->string, "gui_timeout", "Inkscape GUI did not service request within 5 seconds");
        return "{\"type\":\"response\",\"id\":\"" + id->string +
            "\",\"ok\":true,\"result\":" + *result + "}\n";
    }
    if (method->string == "selection.set") {
        auto const *object_ids = params->get("object_ids");
        if (!object_ids || object_ids->type != mcpinkscape::json::Value::Type::array || object_ids->array.size() > 1000) {
            return error_response(id->string, "invalid_request", "selection.set requires object_ids array of at most 1000 IDs");
        }
        std::vector<std::string> ids;
        ids.reserve(object_ids->array.size());
        for (auto const &value : object_ids->array) {
            if (value.type != mcpinkscape::json::Value::Type::string || value.string.empty() || value.string.size() > 128) {
                return error_response(id->string, "invalid_request", "selection IDs must be non-empty strings no longer than 128 bytes");
            }
            ids.push_back(value.string);
        }
        auto const expected_revision = expected_revision_of(&*request);
        if (request->get("expected_revision") && !expected_revision) {
            return error_response(id->string, "invalid_request", "selection revision must be a non-negative integer");
        }
        auto const result = gui_executor.selection_set(std::move(ids), expected_revision);
        if (!result) return error_response(id->string, "gui_timeout", "Inkscape GUI did not service request within 5 seconds");
        if (result->find("{\"error\":\"object_not_found\"") == 0) {
            return error_response(id->string, "object_not_found", "selection target does not exist");
        }
        return mutation_response(id->string, result);
    }
    if (method->string == "object.set_style") {
        auto ids = object_ids_of(params);
        auto style = style_of(params->get("style"));
        auto expected_revision = expected_revision_of(&*request);
        if (!ids || !style || !expected_revision) {
            return error_response(id->string, "invalid_request", "object.set_style requires valid IDs, style, and integer expected_revision");
        }
        return mutation_response(id->string, gui_executor.set_style(std::move(*ids), std::move(*style), *expected_revision));
    }
    if (method->string == "shape.create") {
        auto const *kind = params->get("kind");
        auto const *values = params->get("values");
        auto const *object_id = params->get("object_id");
        auto expected_revision = expected_revision_of(&*request);
        if (!kind || kind->type != mcpinkscape::json::Value::Type::string || !values ||
            values->type != mcpinkscape::json::Value::Type::object || !object_id ||
            object_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(object_id->string) || !expected_revision) {
            return error_response(id->string, "invalid_request", "shape.create requires kind, values, valid object_id, and integer expected_revision");
        }
        std::string element_name;
        std::map<std::string, std::string> attributes = {{"id", object_id->string}, {"data-mcp-created", "true"}};
        auto length = [&] (char const *name) -> bool {
            auto scalar = scalar_value(values->get(name));
            if (!scalar || !valid_svg_length(*scalar)) return false;
            attributes.emplace(name, *scalar);
            return true;
        };
        if (kind->string == "rectangle") {
            element_name = "rect";
            if (!length("x") || !length("y") || !length("width") || !length("height")) {
                return error_response(id->string, "invalid_request", "rectangle requires SVG lengths x, y, width, and height");
            }
            for (auto const *name : {"rx", "ry"}) {
                if (values->get(name) && !length(name)) return error_response(id->string, "invalid_request", "rounded rectangle radius must be an SVG length");
            }
        } else if (kind->string == "ellipse") {
            element_name = "ellipse";
            if (!length("cx") || !length("cy") || !length("rx") || !length("ry")) return error_response(id->string, "invalid_request", "ellipse requires SVG lengths cx, cy, rx, and ry");
        } else if (kind->string == "circle") {
            element_name = "circle";
            if (!length("cx") || !length("cy") || !length("r")) return error_response(id->string, "invalid_request", "circle requires SVG lengths cx, cy, and r");
        } else if (kind->string == "line") {
            element_name = "line";
            if (!length("x1") || !length("y1") || !length("x2") || !length("y2")) return error_response(id->string, "invalid_request", "line requires SVG lengths x1, y1, x2, and y2");
        } else if (kind->string == "polyline" || kind->string == "polygon") {
            auto const *points = values->get("points");
            if (!points || points->type != mcpinkscape::json::Value::Type::array || points->array.size() < 2 || points->array.size() > 10000) {
                return error_response(id->string, "invalid_request", "polyline/polygon requires two through 10000 points");
            }
            std::string encoded;
            for (auto const &point : points->array) {
                if (point.type != mcpinkscape::json::Value::Type::array || point.array.size() != 2 ||
                    point.array[0].type != mcpinkscape::json::Value::Type::number || point.array[1].type != mcpinkscape::json::Value::Type::number) {
                    return error_response(id->string, "invalid_request", "each point must contain two finite numbers");
                }
                if (!encoded.empty()) encoded += ' ';
                encoded += std::to_string(point.array[0].number) + "," + std::to_string(point.array[1].number);
            }
            element_name = kind->string;
            attributes.emplace("points", std::move(encoded));
        } else if (kind->string == "path") {
            auto const *path = values->get("d");
            if (!path || path->type != mcpinkscape::json::Value::Type::string || path->string.empty() || path->string.size() > 65536 ||
                !valid_style_value(path->string)) return error_response(id->string, "invalid_request", "path requires a bounded SVG path string");
            element_name = "path";
            attributes.emplace("d", path->string);
        } else {
            return error_response(id->string, "invalid_request", "unsupported shape kind");
        }
        if (auto const *style = params->get("style")) {
            auto parsed_style = style_of(style);
            if (!parsed_style) return error_response(id->string, "invalid_request", "shape style contains unsupported fields");
            attributes.emplace("style", css_text(*parsed_style));
        }
        std::optional<std::string> layer_id;
        if (auto const *layer = params->get("layer_id")) {
            if (layer->type != mcpinkscape::json::Value::Type::string || !valid_identifier(layer->string)) return error_response(id->string, "invalid_request", "layer_id is invalid");
            layer_id = layer->string;
        }
        return mutation_response(id->string, gui_executor.create_element(std::move(element_name), std::move(attributes), "", std::move(layer_id), *expected_revision, "MCP create shape"));
    }
    if (method->string == "text.create") {
        auto const *text = params->get("text");
        auto const *object_id = params->get("object_id");
        auto x = scalar_value(params->get("x"));
        auto y = scalar_value(params->get("y"));
        auto expected_revision = expected_revision_of(&*request);
        if (!text || text->type != mcpinkscape::json::Value::Type::string || text->string.size() > 65536 ||
            !object_id || object_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(object_id->string) ||
            !x || !y || !valid_svg_length(*x) || !valid_svg_length(*y) || !expected_revision) {
            return error_response(id->string, "invalid_request", "text.create requires bounded text, valid ID, x/y SVG lengths, and integer expected_revision");
        }
        std::map<std::string, std::string> attributes = {{"id", object_id->string}, {"x", *x}, {"y", *y}, {"xml:space", "preserve"}, {"data-mcp-created", "true"}};
        if (auto const *style = params->get("style")) {
            auto parsed_style = style_of(style);
            if (!parsed_style) return error_response(id->string, "invalid_request", "text style contains unsupported fields");
            attributes.emplace("style", css_text(*parsed_style));
        }
        std::optional<std::string> layer_id;
        if (auto const *layer = params->get("layer_id")) {
            if (layer->type != mcpinkscape::json::Value::Type::string || !valid_identifier(layer->string)) return error_response(id->string, "invalid_request", "layer_id is invalid");
            layer_id = layer->string;
        }
        return mutation_response(id->string, gui_executor.create_element("text", std::move(attributes), text->string, std::move(layer_id), *expected_revision, "MCP create text"));
    }
    if (method->string == "text.set") {
        auto const *object_id = params->get("object_id");
        auto const *text = params->get("text");
        auto expected_revision = expected_revision_of(&*request);
        if (!object_id || object_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(object_id->string) ||
            !text || text->type != mcpinkscape::json::Value::Type::string || text->string.size() > 65536 || !expected_revision) {
            return error_response(id->string, "invalid_request", "text.set requires a valid ID, bounded text, and integer expected_revision");
        }
        auto result = gui_executor.set_text(object_id->string, text->string, *expected_revision);
        if (result && result->find("{\"state\":\"not_basic_text\"") == 0) return error_response(id->string, "not_basic_text", "text.set requires a basic SVG text object");
        return mutation_response(id->string, result);
    }
    if (method->string == "document.set_background") {
        auto color = scalar_value(params->get("color"));
        auto const *opacity = params->get("opacity");
        auto expected_revision = expected_revision_of(&*request);
        if (!color || !valid_style_value(*color) || !opacity || opacity->type != mcpinkscape::json::Value::Type::number ||
            opacity->number < 0 || opacity->number > 1 || !expected_revision) {
            return error_response(id->string, "invalid_request", "document.set_background requires colour, opacity 0 through 1, and integer expected_revision");
        }
        return mutation_response(id->string, gui_executor.set_background(*color, opacity->number, *expected_revision));
    }
    if (method->string == "gradient.create") {
        auto const *kind = params->get("kind");
        auto const *gradient_id = params->get("gradient_id");
        auto stops = gradient_stops_of(params->get("stops"));
        auto expected_revision = expected_revision_of(&*request);
        if (!kind || kind->type != mcpinkscape::json::Value::Type::string || (kind->string != "linear" && kind->string != "radial") ||
            !gradient_id || gradient_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(gradient_id->string) || !stops || !expected_revision) {
            return error_response(id->string, "invalid_request", "gradient.create requires linear/radial kind, valid gradient_id, ordered stops, and integer expected_revision");
        }
        auto geometry = gradient_geometry_of(kind->string, params->get("geometry"));
        if (!geometry) return error_response(id->string, "invalid_request", "gradient geometry is invalid");
        return mutation_response(id->string, gui_executor.create_gradient(kind->string, gradient_id->string, std::move(*geometry), std::move(*stops), *expected_revision));
    }
    if (method->string == "gradient.set_stops") {
        auto const *gradient_id = params->get("gradient_id");
        auto stops = gradient_stops_of(params->get("stops"));
        auto expected_revision = expected_revision_of(&*request);
        if (!gradient_id || gradient_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(gradient_id->string) || !stops || !expected_revision) {
            return error_response(id->string, "invalid_request", "gradient.set_stops requires valid gradient_id, ordered stops, and integer expected_revision");
        }
        auto result = gui_executor.set_gradient_stops(gradient_id->string, std::move(*stops), *expected_revision);
        if (result && result->find("{\"state\":\"gradient_not_found\"") == 0) return error_response(id->string, "gradient_not_found", "gradient does not exist");
        return mutation_response(id->string, result);
    }
    if (method->string == "gradient.apply") {
        auto ids = object_ids_of(params);
        auto const *gradient_id = params->get("gradient_id");
        auto const *target = params->get("target");
        auto expected_revision = expected_revision_of(&*request);
        if (!ids || !gradient_id || gradient_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(gradient_id->string) ||
            !target || target->type != mcpinkscape::json::Value::Type::string || (target->string != "fill" && target->string != "stroke") || !expected_revision) {
            return error_response(id->string, "invalid_request", "gradient.apply requires IDs, gradient_id, fill/stroke target, and integer expected_revision");
        }
        auto result = gui_executor.apply_gradient(std::move(*ids), gradient_id->string, target->string, *expected_revision);
        if (result && result->find("{\"state\":\"gradient_not_found\"") == 0) return error_response(id->string, "gradient_not_found", "gradient does not exist");
        return mutation_response(id->string, result);
    }
    if (method->string == "image.import") {
        auto const *staging_path = params->get("staging_path");
        auto const *mime_type = params->get("mime_type");
        auto const *object_id = params->get("object_id");
        auto x = scalar_value(params->get("x"));
        auto y = scalar_value(params->get("y"));
        auto width = scalar_value(params->get("width"));
        auto height = scalar_value(params->get("height"));
        auto expected_revision = expected_revision_of(&*request);
        if (!staging_path || staging_path->type != mcpinkscape::json::Value::Type::string || staging_path->string.size() > 4096 ||
            !mime_type || mime_type->type != mcpinkscape::json::Value::Type::string ||
            !object_id || object_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(object_id->string) ||
            !x || !y || !width || !height || !valid_svg_length(*x) || !valid_svg_length(*y) || !valid_svg_length(*width) || !valid_svg_length(*height) || !expected_revision) {
            return error_response(id->string, "invalid_request", "image.import requires a staged PNG/JPEG, valid ID, SVG geometry, and integer expected_revision");
        }
        auto data_uri = staged_image_data_uri(staging_path->string, mime_type->string);
        if (!data_uri) return error_response(id->string, "invalid_image_staging", "image staging file is invalid, inaccessible, or outside the private bridge import directory");
        std::map<std::string, std::string> attributes = {
            {"id", object_id->string}, {"x", *x}, {"y", *y}, {"width", *width}, {"height", *height},
            {"href", std::move(*data_uri)}, {"preserveAspectRatio", "none"}, {"data-mcp-created", "true"},
        };
        return mutation_response(id->string, gui_executor.create_element("image", std::move(attributes), "", std::nullopt, *expected_revision, "MCP import image"));
    }
    if (method->string == "object.delete" || method->string == "object.duplicate") {
        auto ids = object_ids_of(params);
        auto expected_revision = expected_revision_of(&*request);
        if (!ids || !expected_revision) return error_response(id->string, "invalid_request", "object mutation requires valid IDs and integer expected_revision");
        if (method->string == "object.delete") return mutation_response(id->string, gui_executor.delete_objects(std::move(*ids), *expected_revision));
        return mutation_response(id->string, gui_executor.duplicate_objects(std::move(*ids), *expected_revision));
    }
    if (method->string == "object.group") {
        auto ids = object_ids_of(params);
        auto const *group_id = params->get("group_id");
        auto expected_revision = expected_revision_of(&*request);
        if (!ids || !group_id || group_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(group_id->string) || !expected_revision) {
            return error_response(id->string, "invalid_request", "object.group requires valid IDs, group_id, and integer expected_revision");
        }
        auto result = gui_executor.group_objects(std::move(*ids), group_id->string, *expected_revision);
        if (result && result->find("{\"state\":\"different_parents\"") == 0) return error_response(id->string, "different_parents", "all group targets must be siblings");
        return mutation_response(id->string, result);
    }
    if (method->string == "object.ungroup") {
        auto const *group_id = params->get("group_id");
        auto expected_revision = expected_revision_of(&*request);
        if (!group_id || group_id->type != mcpinkscape::json::Value::Type::string || !valid_identifier(group_id->string) || !expected_revision) {
            return error_response(id->string, "invalid_request", "object.ungroup requires valid group_id and integer expected_revision");
        }
        auto result = gui_executor.ungroup_objects(group_id->string, *expected_revision);
        if (result && result->find("{\"state\":\"group_not_found\"") == 0) return error_response(id->string, "group_not_found", "group does not exist");
        return mutation_response(id->string, result);
    }
    if (method->string == "object.raise" || method->string == "object.lower") {
        auto ids = object_ids_of(params);
        auto expected_revision = expected_revision_of(&*request);
        if (!ids || !expected_revision) return error_response(id->string, "invalid_request", "stacking requires valid IDs and integer expected_revision");
        auto result = gui_executor.reorder_objects(std::move(*ids), method->string == "object.raise", *expected_revision);
        if (result && result->find("{\"state\":\"different_parents\"") == 0) return error_response(id->string, "different_parents", "all stacking targets must be siblings");
        return mutation_response(id->string, result);
    }
    if (method->string == "snapshot.render") {
        auto const *area = params->get("area");
        if (!area || area->type != mcpinkscape::json::Value::Type::string || (area->string != "page" && area->string != "drawing")) {
            return error_response(id->string, "invalid_request", "snapshot.render requires area page or drawing");
        }
        auto result = gui_executor.render_snapshot(area->string);
        if (result && (result->starts_with("{\"state\":\"stale_document_target\"") ||
                       result->starts_with("{\"state\":\"document_not_displayed\""))) {
            return mutation_response(id->string, result);
        }
        if (result && result->find("{\"state\":\"no_document\"") == 0) return error_response(id->string, "no_document", "no active Inkscape document");
        if (result && result->find("{\"state\":\"snapshot_unavailable\"") == 0) return error_response(id->string, "snapshot_failed", "native snapshot staging directory is unavailable");
        if (result && result->find("{\"state\":\"snapshot_directory_create_failed\"") == 0) return error_response(id->string, "snapshot_failed", "native snapshot staging directory could not be created");
        if (result && result->find("{\"state\":\"snapshot_directory_permissions_failed\"") == 0) return error_response(id->string, "snapshot_failed", "native snapshot staging directory permissions could not be set");
        if (result && result->find("{\"state\":\"snapshot_save_failed\"") == 0) return error_response(id->string, "snapshot_failed", "native snapshot SVG staging failed");
        if (result && result->find("{\"state\":\"snapshot_spawn_failed\"") == 0) return error_response(id->string, "snapshot_failed", "native snapshot renderer could not start");
        if (result && result->find("{\"state\":\"snapshot_renderer_exit\"") == 0) return error_response(id->string, "snapshot_failed", "native snapshot renderer returned a non-zero status");
        if (result && result->find("{\"state\":\"snapshot_output_missing\"") == 0) return error_response(id->string, "snapshot_failed", "native snapshot renderer returned no PNG");
        if (result && result->find("{\"state\":") == 0) return error_response(id->string, "snapshot_failed", "native snapshot staging or rendering failed");
        return mutation_response(id->string, result);
    }
    if (method->string == "snapshot.release") {
        auto const *snapshot_id = params->get("snapshot_id");
        if (!snapshot_id || snapshot_id->type != mcpinkscape::json::Value::Type::string || snapshot_id->string.size() > 128) return error_response(id->string, "invalid_request", "snapshot.release requires snapshot_id");
        auto result = gui_executor.release_snapshot(snapshot_id->string);
        if (result && result->find("{\"state\":\"snapshot_not_found\"") == 0) return error_response(id->string, "snapshot_not_found", "snapshot does not exist");
        return mutation_response(id->string, result);
    }
    if (method->string == "object.move" || method->string == "object.rotate" || method->string == "object.scale") {
        auto ids = object_ids_of(params);
        auto expected_revision = expected_revision_of(&*request);
        if (!ids || !expected_revision) return error_response(id->string, "invalid_request", "transform requires valid IDs and integer expected_revision");
        auto number = [&] (char const *name) -> std::optional<double> {
            auto const *value = params->get(name);
            if (!value || value->type != mcpinkscape::json::Value::Type::number) return std::nullopt;
            return value->number;
        };
        std::string transform_text;
        std::string undo_label;
        if (method->string == "object.move") {
            auto dx = number("dx"); auto dy = number("dy");
            if (!dx || !dy) return error_response(id->string, "invalid_request", "object.move requires finite dx and dy");
            transform_text = "translate(" + std::to_string(*dx) + "," + std::to_string(*dy) + ")";
            undo_label = "MCP move objects";
        } else if (method->string == "object.rotate") {
            auto degrees = number("degrees");
            if (!degrees) return error_response(id->string, "invalid_request", "object.rotate requires finite degrees");
            transform_text = "rotate(" + std::to_string(*degrees) + ")";
            undo_label = "MCP rotate objects";
        } else {
            auto scale_x = number("scale_x"); auto scale_y = number("scale_y");
            if (!scale_x || !scale_y || *scale_x == 0 || *scale_y == 0) return error_response(id->string, "invalid_request", "object.scale requires non-zero finite scale_x and scale_y");
            transform_text = "scale(" + std::to_string(*scale_x) + "," + std::to_string(*scale_y) + ")";
            undo_label = "MCP scale objects";
        }
        return mutation_response(id->string, gui_executor.transform(std::move(*ids), std::move(transform_text), *expected_revision, std::move(undo_label)));
    }
    return error_response(id->string, "unsupported_method", "method is not implemented by this bridge build");
}

std::string response_for_request(std::string const &line)
{
    auto const response = response_for_request_impl(line);
    std::string parse_error;
    auto parsed = mcpinkscape::json::Parser(response).parse(parse_error);
    if (parsed) if (auto const *result = parsed->get("result")) if (auto const *state = result->get("state")) {
        auto const *id = parsed->get("id");
        if (id && state->type == mcpinkscape::json::Value::Type::string) {
            if (state->string == "stale_document_target") return error_response(id->string, "stale_document_target", "application instance changed or document was closed; enumerate targets again");
            if (state->string == "document_not_displayed") return error_response(id->string, "document_not_displayed", "target document has no available desktop");
        }
    }
    return response;
}

class BridgeServer final {
public:
    bool start()
    {
#ifdef _WIN32
        return start_windows();
#else
        if (_running.load()) return true;

        _state_directory = bridge_state_directory();
        if (g_mkdir_with_parents(_state_directory.c_str(), 0700) != 0) {
            g_warning("mcpInkscape bridge: cannot create state directory: %s", g_strerror(errno));
            return false;
        }
        if (chmod(_state_directory.c_str(), 0700) != 0) {
            g_warning("mcpInkscape bridge: cannot protect state directory: %s", g_strerror(errno));
            return false;
        }

        _socket_path = _state_directory + "/bridge.sock";
        if (auto const *endpoint = g_getenv("MCPINKSCAPE_BRIDGE_SOCKET")) _socket_path = endpoint;
        unlink(_socket_path.c_str());
        _listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (_listen_fd < 0) {
            g_warning("mcpInkscape bridge: cannot create UDS: %s", g_strerror(errno));
            return false;
        }
        sockaddr_un address{};
        address.sun_family = AF_UNIX;
        if (_socket_path.size() >= sizeof(address.sun_path)) {
            g_warning("mcpInkscape bridge: UDS path is too long");
            close(_listen_fd);
            _listen_fd = -1;
            return false;
        }
        std::strncpy(address.sun_path, _socket_path.c_str(), sizeof(address.sun_path) - 1);
        if (bind(_listen_fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0 ||
            chmod(_socket_path.c_str(), 0600) != 0 || listen(_listen_fd, 4) != 0) {
            g_warning("mcpInkscape bridge: cannot bind UDS: %s", g_strerror(errno));
            close(_listen_fd);
            _listen_fd = -1;
            unlink(_socket_path.c_str());
            return false;
        }
        _running.store(true);
        _thread = std::thread([this, listener = _listen_fd] { serve(listener); });
        g_message("mcpInkscape bridge started at %s", _socket_path.c_str());
        return true;
#endif
    }

    void stop()
    {
#ifdef _WIN32
        stop_windows();
#else
        if (!_running.exchange(false)) return;
        shutdown(_listen_fd, SHUT_RDWR);
        close(_listen_fd);
        _listen_fd = -1;
        {
            std::lock_guard<std::mutex> lock(_client_mutex);
            // Wake an idle connected client before joining its socket worker.
            // The worker owns close(), keeping descriptor reuse out of this race.
            if (_client_fd >= 0) shutdown(_client_fd, SHUT_RDWR);
        }
        if (_thread.joinable()) _thread.join();
        unlink(_socket_path.c_str());
        g_message("mcpInkscape bridge stopped");
#endif
    }

    bool running() const { return _running.load(); }

    ~BridgeServer() { stop(); }

private:
#ifndef _WIN32
    bool peer_is_current_user(int client) const
    {
#if defined(__FreeBSD__)
        uid_t peer_uid = 0;
        gid_t peer_gid = 0;
        if (getpeereid(client, &peer_uid, &peer_gid) != 0) {
            g_warning("mcpInkscape bridge: cannot obtain UDS peer credentials: %s", g_strerror(errno));
            return false;
        }
        if (peer_uid != getuid()) {
            g_warning("mcpInkscape bridge: rejected UDS peer with different UID");
            return false;
        }
        return true;
#elif defined(__linux__)
        struct ucred credentials{};
        socklen_t size = sizeof(credentials);
        if (getsockopt(client, SOL_SOCKET, SO_PEERCRED, &credentials, &size) != 0) {
            g_warning("mcpInkscape bridge: cannot obtain UDS peer credentials: %s", g_strerror(errno));
            return false;
        }
        if (credentials.uid != getuid()) {
            g_warning("mcpInkscape bridge: rejected UDS peer with different UID");
            return false;
        }
        return true;
#else
        g_warning("mcpInkscape bridge: this Unix platform has no configured peer-credential check");
        return false;
#endif
    }

    void serve(int listener)
    {
        while (_running.load()) {
            auto client = accept(listener, nullptr, nullptr);
            if (client < 0) {
                if (_running.load()) g_warning("mcpInkscape bridge: accept failed: %s", g_strerror(errno));
                continue;
            }
            if (!peer_is_current_user(client)) {
                close(client);
                continue;
            }
            {
                std::lock_guard<std::mutex> lock(_client_mutex);
                if (!_running.load()) {
                    close(client);
                    break;
                }
                _client_fd = client;
            }
            serve_client(client);
            {
                std::lock_guard<std::mutex> lock(_client_mutex);
                _client_fd = -1;
                close(client);
            }
        }
    }

    void serve_client(int client)
    {
        std::string line;
        char byte = 0;
        while (_running.load() && recv(client, &byte, 1, 0) == 1) {
            if (byte == '\n') {
                respond(client, line);
                line.clear();
                continue;
            }
            if (line.size() >= MAX_LINE_BYTES) {
                auto const body = error_response("", "message_too_large", "bridge message exceeds 1 MiB");
                send(client, body.data(), body.size(), 0);
                return;
            }
            line += byte;
        }
    }

    void respond(int client, std::string const &line)
    {
        auto const body = response_for_request(line);
        send(client, body.data(), body.size(), 0);
        quit_after_response();
    }

    std::atomic_bool _running = false;
    int _listen_fd = -1;
    int _client_fd = -1;
    std::mutex _client_mutex;
    std::string _state_directory;
    std::string _socket_path;
    std::thread _thread;
#else
    bool start_windows()
    {
        if (_running.load()) return true;
        WSADATA winsock_data{};
        if (WSAStartup(MAKEWORD(2, 2), &winsock_data) != 0) {
            g_warning("mcpInkscape bridge: WSAStartup failed");
            return false;
        }
        auto port = 61779L;
        if (auto const *configured_port = std::getenv("MCPINKSCAPE_BRIDGE_PORT")) {
            port = std::strtol(configured_port, nullptr, 10);
        }
        if (port < 1 || port > 65535) {
            g_warning("mcpInkscape bridge: MCPINKSCAPE_BRIDGE_PORT is invalid");
            WSACleanup();
            return false;
        }
        _listen_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (_listen_socket == INVALID_SOCKET) {
            g_warning("mcpInkscape bridge: cannot create loopback TCP socket");
            WSACleanup();
            return false;
        }
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_port = htons(static_cast<u_short>(port));
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        if (bind(_listen_socket, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0 ||
            listen(_listen_socket, 4) != 0) {
            g_warning("mcpInkscape bridge: cannot bind 127.0.0.1:%ld", port);
            closesocket(_listen_socket);
            _listen_socket = INVALID_SOCKET;
            WSACleanup();
            return false;
        }
        _running.store(true);
        _thread = std::thread([this, listener = _listen_socket] { serve_windows(listener); });
        g_message("mcpInkscape bridge started at 127.0.0.1:%ld", port);
        return true;
    }

    void stop_windows()
    {
        if (!_running.exchange(false)) return;
        shutdown(_listen_socket, SD_BOTH);
        closesocket(_listen_socket);
        _listen_socket = INVALID_SOCKET;
        {
            std::lock_guard<std::mutex> lock(_client_mutex);
            if (_client_socket != INVALID_SOCKET) shutdown(_client_socket, SD_BOTH);
        }
        if (_thread.joinable()) _thread.join();
        WSACleanup();
        g_message("mcpInkscape bridge stopped");
    }

    void serve_windows(SOCKET listener)
    {
        while (_running.load()) {
            auto client = accept(listener, nullptr, nullptr);
            if (client == INVALID_SOCKET) continue;
            {
                std::lock_guard<std::mutex> lock(_client_mutex);
                if (!_running.load()) {
                    closesocket(client);
                    break;
                }
                _client_socket = client;
            }
            serve_client_windows(client);
            {
                std::lock_guard<std::mutex> lock(_client_mutex);
                _client_socket = INVALID_SOCKET;
                closesocket(client);
            }
        }
    }

    void serve_client_windows(SOCKET client)
    {
        std::string line;
        char byte = 0;
        while (_running.load() && recv(client, &byte, 1, 0) == 1) {
            if (byte == '\n') {
                respond_windows(client, line);
                line.clear();
                continue;
            }
            if (line.size() >= MAX_LINE_BYTES) {
                auto const body = error_response("", "message_too_large", "bridge message exceeds 1 MiB");
                send(client, body.data(), static_cast<int>(body.size()), 0);
                return;
            }
            line += byte;
        }
    }

    void respond_windows(SOCKET client, std::string const &line)
    {
        auto const body = response_for_request(line);
        send(client, body.data(), static_cast<int>(body.size()), 0);
        quit_after_response();
    }

    std::atomic_bool _running = false;
    SOCKET _listen_socket = INVALID_SOCKET;
    SOCKET _client_socket = INVALID_SOCKET;
    std::mutex _client_mutex;
    std::thread _thread;
#endif
};

BridgeServer bridge_server;

class BridgeExtension final : public Inkscape::Extension::Implementation::Implementation {
public:
    bool load(Inkscape::Extension::Extension *) override { return true; }

#if defined(MCPINKSCAPE_EFFECT_API_14)
    void effect(
        Inkscape::Extension::Effect *module,
        SPDesktop *,
        Inkscape::Extension::Implementation::ImplementationDocumentCache *
    ) override
#else
    void effect(
        Inkscape::Extension::Effect *module,
        Inkscape::Extension::ExecutionEnv *,
        SPDesktop *,
        Inkscape::Extension::Implementation::ImplementationDocumentCache *
    ) override
#endif
    {
        auto const id = std::string(module->get_id());
        if (id == "org.mcpinkscape.bridge.start") {
            bridge_server.start();
        } else if (id == "org.mcpinkscape.bridge.stop") {
            bridge_server.stop();
        } else if (id == "org.mcpinkscape.bridge.status") {
            g_message("mcpInkscape bridge is %s", bridge_server.running() ? "running" : "stopped");
        }
    }
};

} // namespace

extern "C" G_MODULE_EXPORT Inkscape::Extension::Implementation::Implementation *GetImplementation()
{
    return new BridgeExtension();
}

extern "C" G_MODULE_EXPORT char const *GetInkscapeVersion()
{
    return Inkscape::version_string;
}
