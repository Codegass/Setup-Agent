package benchmark;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.util.*;
import org.apache.maven.eventspy.AbstractEventSpy;
import org.apache.maven.execution.ExecutionEvent;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.plugin.PluginParameterExpressionEvaluator;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Passive observations only: no goal/configuration mutation or success decisions. */
public final class MavenWitness extends AbstractEventSpy {
    private Path base, root;
    private BufferedWriter output;
    private long sequence;
    private final IdentityHashMap<MojoExecution, Deque<String>> active = new IdentityHashMap<>();

    public void init(Context context) throws Exception {
        String destination = System.getenv("BENCH_MAVEN_WITNESS_DIR");
        if (destination == null) return;
        root = Paths.get(System.getenv("BENCH_MAVEN_WITNESS_ROOT")).toRealPath();
        base = Paths.get(destination).resolve(UUID.randomUUID().toString());
        Files.createDirectories(base);
        output = Files.newBufferedWriter(base.resolve("events.jsonl"), StandardCharsets.UTF_8,
                                         StandardOpenOption.CREATE_NEW);
        emit(map("event", "ObserverStarted", "root", root.toString(),
                 "run_id", System.getenv("BENCH_MAVEN_WITNESS_RUN_ID"),
                 "dispatch", System.getenv("BENCH_MAVEN_WITNESS_DISPATCH"),
                 "launcher_observation", System.getenv("BENCH_MAVEN_WITNESS_LAUNCH"),
                 "started_epoch_ms", System.currentTimeMillis(),
                 "process_id", java.lang.management.ManagementFactory.getRuntimeMXBean().getName().split("@")[0],
                 "java_home", System.getProperty("java.home"),
                 "java_version", System.getProperty("java.version"),
                 "java_vendor", System.getProperty("java.vendor")));
    }

    public synchronized void onEvent(Object value) throws Exception {
        if (output == null || !(value instanceof ExecutionEvent)) return;
        ExecutionEvent event = (ExecutionEvent) value;
        String type = event.getType().name();
        Map<String, Object> row = map("event", type, "sequence", ++sequence,
                                     "thread", Thread.currentThread().getId());
        String log = System.getenv("BENCH_MAVEN_WITNESS_LOG");
        if (log != null) row.put("log_offset", Files.size(Paths.get(log)));
        if (type.equals("SessionStarted")) {
            row.put("requested_goals", event.getSession().getRequest().getGoals());
            row.put("concurrency", event.getSession().getRequest().getDegreeOfConcurrency());
            row.put("maven_version", event.getSession().getSystemProperties().getProperty("maven.version"));
        }
        MavenProject project = event.getProject();
        if (project != null) {
            row.put("module", project.getArtifactId());
            row.put("module_path", relative(project.getBasedir().toPath()));
        }
        MojoExecution mojo = event.getMojoExecution();
        if (mojo != null) {
            row.put("plugin", mojo.getGroupId()+":"+mojo.getArtifactId());
            row.put("version", mojo.getVersion()); row.put("goal", mojo.getGoal());
            row.put("execution", mojo.getExecutionId());
            if (mojo.getMojoDescriptor() != null)
                row.put("prefix", mojo.getMojoDescriptor().getPluginDescriptor().getGoalPrefix());
            Deque<String> stack = active.get(mojo);
            if (type.equals("MojoStarted")) {
                if (stack == null) { stack = new ArrayDeque<>(); active.put(mojo, stack); }
                stack.push("mojo-"+sequence);
            }
            String id = stack == null || stack.isEmpty() ? null : stack.peek();
            row.put("mojo_id", id);
            if (id != null && (type.equals("MojoStarted") || type.equals("MojoSucceeded") || type.equals("MojoFailed"))) {
                String boundary = type.equals("MojoStarted") ? "before" : "after";
                try { row.put("files", snapshot(event, base.resolve(id+"-"+boundary))); }
                catch (Exception ex) { row.put("collection_error", ex.getClass().getSimpleName()+": "+ex.getMessage()); }
                if (mojo.getArtifactId().equals("maven-compiler-plugin")) {
                    PluginParameterExpressionEvaluator evaluator = new PluginParameterExpressionEvaluator(event.getSession(), mojo);
                    Object fork = configuredValue(mojo, evaluator, "fork");
                    Object toolchain = configuredValue(mojo, evaluator, "jdkToolchain");
                    Object executable = configuredValue(mojo, evaluator, "executable");
                    Object compiler = configuredValue(mojo, evaluator, "compilerId");
                    row.put("compiler_runtime_observed", "false".equals(String.valueOf(fork))
                        && toolchain == null && executable == null && "javac".equals(String.valueOf(compiler)));
                    try { row.put("compiler_inputs", compilerInputs(event)); }
                    catch (Exception ex) { row.put("input_collection_error", ex.getClass().getSimpleName()+": "+ex.getMessage()); }
                }
                Xpp3Dom config = mojo.getConfiguration();
                Map<String,Object> skips = new TreeMap<>();
                if (config != null) for (Xpp3Dom parameter : config.getChildren()) {
                    if (parameter.getName().toLowerCase(Locale.ROOT).startsWith("skip")) {
                        try { skips.put(parameter.getName(), stableValue(configuredValue(mojo,
                            new PluginParameterExpressionEvaluator(event.getSession(), mojo), parameter.getName()))); }
                        catch (Exception ex) { skips.put(parameter.getName(), "unresolved"); }
                    }
                }
                row.put("skip_parameters", skips);
                if (mojo.getArtifactId().equals("maven-surefire-plugin") || mojo.getArtifactId().equals("maven-failsafe-plugin")) {
                    List<String> checksums = new ArrayList<>();
                    Map<?,?> context = event.getSession().getPluginContext(mojo.getMojoDescriptor().getPluginDescriptor(), project);
                    for (Map.Entry<?,?> item : context.entrySet()) {
                        if (item.getKey() instanceof String && item.getKey().equals(item.getValue())
                                && ((String)item.getKey()).matches("[0-9A-F]{40}")) checksums.add((String)item.getKey());
                    }
                    Collections.sort(checksums); row.put("test_execution_checksums", checksums);
                }
                row.put("configuration_sha256", digest((config == null ? "" : config.toString()).getBytes(StandardCharsets.UTF_8)));
                try {
                    Object resolved = resolvedConfiguration(config, new PluginParameterExpressionEvaluator(event.getSession(), mojo), mojo);
                    row.put("resolved_configuration_sha256", digest(json(resolved).getBytes(StandardCharsets.UTF_8)));
                    if ("true".equals(System.getenv("BENCH_SYNTHETIC_CONFIG_DEBUG"))) row.put("synthetic_configuration_debug", resolved);
                } catch (Exception ex) { row.put("configuration_error", ex.getClass().getSimpleName()+": "+ex.getMessage()); }
            }
            if (stack != null && !stack.isEmpty() && (type.equals("MojoSucceeded") || type.equals("MojoFailed"))) stack.pop();
        }
        emit(row);
    }

    private Object resolvedConfiguration(Xpp3Dom node, PluginParameterExpressionEvaluator evaluator, MojoExecution mojo) throws Exception {
        if (node == null) return null;
        List<Object> children = new ArrayList<>();
        for (Xpp3Dom child : node.getChildren()) children.add(resolvedConfiguration(child, evaluator, mojo));
        String value = node.getValue();
        Object evaluated = value == null ? null : evaluator.evaluate(value);
        if (evaluated == null && node.getAttribute("default-value") != null)
            evaluated = evaluator.evaluate(node.getAttribute("default-value"));
        if (mojo.getArtifactId().equals("maven-compiler-plugin") && node.getName().equals("compileSourceRoots") && evaluated instanceof Collection) {
            List<Object> roots = new ArrayList<>((Collection<?>) evaluated);
            Object generated = generatedDirectory(mojo, evaluator);
            if (generated != null && !roots.contains(generated.toString())) roots.add(generated.toString());
            evaluated = roots;
        }
        return map("name", node.getName(), "value", stableValue(evaluated), "children", children);
    }

    private Object stableValue(Object value) throws Exception {
        if (value == null || value instanceof String || value instanceof Number || value instanceof Boolean) return value;
        if (value instanceof Collection) { List<Object> values = new ArrayList<>(); for (Object item : (Collection<?>)value) values.add(stableValue(item)); return values; }
        if (value instanceof Map) { Map<String,Object> values = new TreeMap<>(); for (Map.Entry<?,?> item : ((Map<?,?>)value).entrySet()) values.put(item.getKey().toString(), stableValue(item.getValue())); return values; }
        if (value instanceof org.apache.maven.execution.MavenSession) {
            org.apache.maven.execution.MavenSession session = (org.apache.maven.execution.MavenSession)value;
            return map("reference", "MavenSession", "project", session.getCurrentProject().getId(),
                "repository", session.getLocalRepository().getBasedir(), "offline", session.isOffline());
        }
        if (value instanceof MavenProject) { MavenProject project=(MavenProject)value;
            return map("reference", "MavenProject", "identity", project.getId(), "file", project.getFile().toString()); }
        if (value instanceof MojoExecution) { MojoExecution mojo=(MojoExecution)value;
            return map("reference", "MojoExecution", "plugin", mojo.getGroupId()+":"+mojo.getArtifactId(),
                       "version", mojo.getVersion(), "goal", mojo.getGoal(), "execution", mojo.getExecutionId()); }
        if (value instanceof org.apache.maven.artifact.Artifact) {
            org.apache.maven.artifact.Artifact artifact=(org.apache.maven.artifact.Artifact)value;
            return map("reference", "Artifact", "identity", artifact.getId(), "scope", artifact.getScope());
        }
        if (value instanceof File || value instanceof java.net.URI || value instanceof java.net.URL || value instanceof Enum) return value.toString();
        throw new IOException("Unreviewed configuration object: "+value.getClass().getName());
    }

    private Object generatedDirectory(MojoExecution mojo, PluginParameterExpressionEvaluator evaluator) throws Exception {
        String key = mojo.getGoal().equals("testCompile") ? "generatedTestSourcesDirectory" : "generatedSourcesDirectory";
        Xpp3Dom node = mojo.getConfiguration() == null ? null : mojo.getConfiguration().getChild(key);
        if (node == null) return null;
        Object value = node.getValue() == null ? null : evaluator.evaluate(node.getValue());
        return value != null ? value : node.getAttribute("default-value") == null ? null : evaluator.evaluate(node.getAttribute("default-value"));
    }

    private List<Object> compilerInputs(ExecutionEvent event) throws Exception {
        MavenProject project = event.getProject();
        boolean tests = event.getMojoExecution().getGoal().equals("testCompile");
        PluginParameterExpressionEvaluator evaluator = new PluginParameterExpressionEvaluator(event.getSession(), event.getMojoExecution());
        List<String> sources = configuredPaths(event.getMojoExecution(), evaluator, "compileSourceRoots");
        Object generated = generatedDirectory(event.getMojoExecution(), evaluator);
        if (generated != null && !sources.contains(generated.toString())) sources.add(generated.toString());
        List<String> classpath = configuredPaths(event.getMojoExecution(), evaluator, tests ? "testPath" : "compilePath");
        Object output = configuredValue(event.getMojoExecution(), evaluator, "outputDirectory");
        if (output == null) throw new IOException("Compiler output directory was not resolved");
        Path ownOutput = Paths.get(output.toString()).toAbsolutePath().normalize();
        List<Object> result = new ArrayList<>();
        TreeMap<String, Path> paths = new TreeMap<>();
        for (String name : sources) collectInput(Paths.get(name), "source", paths, result);
        for (String name : classpath) {
            Path path = Paths.get(name).toAbsolutePath().normalize();
            if (!path.equals(ownOutput)) collectInput(path, "classpath", paths, result);
        }
        for (MavenProject parent = project; parent != null; parent = parent.getParent())
            if (parent.getFile() != null) collectInput(parent.getFile().toPath(), "model", paths, result);
        for (Map.Entry<String,Path> item : paths.entrySet()) {
            Path path = item.getValue();
            BasicFileAttributes before = Files.readAttributes(path, BasicFileAttributes.class);
            String hash = digestFile(path);
            BasicFileAttributes after = Files.readAttributes(path, BasicFileAttributes.class);
            if (before.size() != after.size() || !before.lastModifiedTime().equals(after.lastModifiedTime()))
                throw new IOException("Compiler input changed during observation");
            result.add(map("path", item.getKey(), "sha256", hash, "bytes", after.size()));
        }
        return result;
    }

    private Object configuredValue(MojoExecution mojo, PluginParameterExpressionEvaluator evaluator, String key) throws Exception {
        Xpp3Dom node = mojo.getConfiguration() == null ? null : mojo.getConfiguration().getChild(key);
        if (node == null) return null;
        Object value = node.getValue() == null ? null : evaluator.evaluate(node.getValue());
        return value != null ? value : node.getAttribute("default-value") == null ? null : evaluator.evaluate(node.getAttribute("default-value"));
    }

    private List<String> configuredPaths(MojoExecution mojo, PluginParameterExpressionEvaluator evaluator, String key) throws Exception {
        Xpp3Dom node = mojo.getConfiguration() == null ? null : mojo.getConfiguration().getChild(key);
        Object value = configuredValue(mojo, evaluator, key);
        List<String> result = new ArrayList<>();
        if (value instanceof Collection) {
            for (Object item : (Collection<?>)value) {
                if (!(item instanceof String || item instanceof File)) throw new IOException("Unresolved compiler path element");
                result.add(item.toString());
            }
            return result;
        }
        if (node != null && node.getChildCount() > 0) {
            for (Xpp3Dom child : node.getChildren()) {
                Object item = evaluator.evaluate(child.getValue());
                if (!(item instanceof String || item instanceof File)) throw new IOException("Unresolved compiler path element");
                result.add(item.toString());
            }
            return result;
        }
        throw new IOException("Compiler path parameter was not resolved: "+key);
    }

    private void collectInput(Path path, String kind, final TreeMap<String,Path> files, List<Object> roots) throws IOException {
        path = path.toAbsolutePath().normalize();
        roots.add(map("root", path.toString(), "kind", kind,
            "state", Files.isDirectory(path) ? "directory" : Files.isRegularFile(path) ? "file" : "absent"));
        if (!Files.exists(path)) return;
        if (Files.isSymbolicLink(path)) throw new IOException("Symlink compiler input");
        if (Files.isRegularFile(path)) { files.put(path.toString(), path); return; }
        Files.walkFileTree(path, new SimpleFileVisitor<Path>() {
            public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                if (!attrs.isRegularFile() || attrs.isSymbolicLink()) throw new IOException("Nonregular compiler input");
                files.put(file.toString(), file); return FileVisitResult.CONTINUE;
            }
        });
    }

    private List<Object> snapshot(ExecutionEvent event, Path destination) throws Exception {
        MojoExecution mojo = event.getMojoExecution();
        MavenProject project = event.getProject();
        boolean reports = mojo.getArtifactId().equals("maven-surefire-plugin") || mojo.getArtifactId().equals("maven-failsafe-plugin");
        boolean compiler = mojo.getArtifactId().equals("maven-compiler-plugin");
        if (!reports && !compiler) return Collections.emptyList();
        Path directory;
        if (reports) {
            Xpp3Dom node = mojo.getConfiguration() == null ? null : mojo.getConfiguration().getChild("reportsDirectory");
            Object resolved = node == null ? null : new PluginParameterExpressionEvaluator(event.getSession(), mojo).evaluate(node.getValue());
            directory = resolved == null ? Paths.get(project.getBuild().getDirectory(),
                mojo.getArtifactId().equals("maven-surefire-plugin") ? "surefire-reports" : "failsafe-reports") : Paths.get(resolved.toString());
        } else {
            Object resolved = configuredValue(mojo, new PluginParameterExpressionEvaluator(event.getSession(), mojo), "outputDirectory");
            if (resolved == null) throw new IOException("Compiler output directory is unresolved");
            directory = Paths.get(resolved.toString());
        }
        if (!directory.isAbsolute()) directory = project.getBasedir().toPath().resolve(directory);
        relative(directory);
        if (!Files.exists(directory)) return Collections.emptyList();
        final boolean copyReports = reports;
        final List<Path> paths = new ArrayList<>();
        Files.walkFileTree(directory, new SimpleFileVisitor<Path>() {
            public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) throws IOException {
                relative(dir); return FileVisitResult.CONTINUE;
            }
            public FileVisitResult visitFile(Path path, BasicFileAttributes attrs) throws IOException {
                relative(path);
                if (attrs.isSymbolicLink() || !attrs.isRegularFile()) throw new IOException("Nonregular observed output");
                if (path.toString().endsWith(copyReports ? ".xml" : ".class")) paths.add(path);
                return FileVisitResult.CONTINUE;
            }
        });
        Collections.sort(paths);
        List<Object> files = new ArrayList<>();
        for (Path file : paths) {
            BasicFileAttributes before = Files.readAttributes(file, BasicFileAttributes.class);
            byte[] bytes = Files.readAllBytes(file);
            BasicFileAttributes after = Files.readAttributes(file, BasicFileAttributes.class);
            if (before.size() != after.size() || !before.lastModifiedTime().equals(after.lastModifiedTime()))
                throw new IOException("Output changed during observation");
            Map<String,Object> item = map("path", relative(file), "sha256", digest(bytes),
                "bytes", bytes.length, "mtime", after.lastModifiedTime().toString());
            if (reports) {
                Path target = destination.resolve(relative(file)); Files.createDirectories(target.getParent());
                Files.write(target, bytes, StandardOpenOption.CREATE_NEW); item.put("archive_path", base.relativize(target).toString());
            } else if (bytes.length >= 8) item.put("class_header", hex(Arrays.copyOf(bytes, 8)));
            files.add(item);
        }
        return files;
    }

    private String relative(Path path) throws IOException {
        Path normalized = path.toAbsolutePath().normalize();
        if (!normalized.startsWith(root) || Files.exists(path) && !path.toRealPath().startsWith(root))
            throw new IOException("Output escapes observed checkout");
        for (Path current = normalized; current != null && current.startsWith(root); current = current.getParent())
            if (Files.isSymbolicLink(current)) throw new IOException("Symlink in observed output");
        return root.relativize(normalized).toString();
    }
    private void emit(Map<String,Object> row) throws IOException { output.write(json(row)); output.newLine(); output.flush(); }
    public void close() throws Exception { if (output != null) { emit(map("event", "ObserverClosed", "closed_epoch_ms", System.currentTimeMillis())); output.close(); } }
    private static Map<String,Object> map(Object... args) { Map<String,Object> out = new LinkedHashMap<>(); for(int i=0;i<args.length;i+=2) out.put((String)args[i],args[i+1]); return out; }
    private static String digest(byte[] bytes) throws Exception { return hex(MessageDigest.getInstance("SHA-256").digest(bytes)); }
    private static String digestFile(Path path) throws Exception {
        MessageDigest hash = MessageDigest.getInstance("SHA-256");
        try (InputStream input = Files.newInputStream(path)) {
            byte[] buffer = new byte[65536]; int count;
            while ((count = input.read(buffer)) >= 0) if (count > 0) hash.update(buffer, 0, count);
        }
        return hex(hash.digest());
    }
    private static String hex(byte[] bytes) { StringBuilder out=new StringBuilder(); for(byte b:bytes) out.append(String.format("%02x",b&255)); return out.toString(); }
    private static String json(Object value) {
        if (value == null) return "null";
        if (value instanceof Number || value instanceof Boolean) return value.toString();
        if (value instanceof Map) { List<String> parts=new ArrayList<>(); for(Object entry:((Map<?,?>)value).entrySet()) { Map.Entry<?,?> e=(Map.Entry<?,?>)entry; parts.add(json(e.getKey())+":"+json(e.getValue())); } return "{"+String.join(",",parts)+"}"; }
        if (value instanceof List) { List<String> parts=new ArrayList<>(); for(Object item:(List<?>)value) parts.add(json(item)); return "["+String.join(",",parts)+"]"; }
        StringBuilder out=new StringBuilder("\""); for(char c:value.toString().toCharArray()) { if(c=='"'||c=='\\') out.append('\\').append(c); else if(c<32) out.append(String.format("\\u%04x",(int)c)); else out.append(c); } return out.append('"').toString();
    }
}
