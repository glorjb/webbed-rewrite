// Applies labels.json (from yyc_labels.py) and decompiles selected functions.
// Script args: <labels.json> <out_dir> <comma-separated function-name regexes>
//@category DesktopPlayground
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.*;
import ghidra.app.cmd.function.CreateFunctionCmd;
import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.regex.*;

public class ApplyYYCLabels extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String json = Files.readString(Path.of(args[0]));
        Path outDir = Path.of(args[1]);
        Files.createDirectories(outDir);
        List<Pattern> wanted = new ArrayList<>();
        for (int i = 2; i < args.length; i++)
            for (String s : args[i].split(",")) if (!s.isEmpty()) wanted.add(Pattern.compile(s));

        // minimal parse of "0xADDR": ["label", "kind"]
        Matcher m = Pattern.compile("\"(0x[0-9a-f]+)\":\\s*\\[\\s*\"([^\"]+)\",\\s*\"([^\"]+)\"\\s*\\]").matcher(json);
        SymbolTable st = currentProgram.getSymbolTable();
        int n = 0, created = 0;
        while (m.find()) {
            Address a = toAddr(Long.parseLong(m.group(1).substring(2), 16));
            String label = m.group(2).replaceAll("[^A-Za-z0-9_@$.]", "_");
            String kind = m.group(3);
            boolean isCode = kind.equals("gml") || kind.equals("builtin") || kind.equals("builtinvar");
            if (isCode) {
                Function f = getFunctionAt(a);
                if (f == null) {
                    disassemble(a);
                    new CreateFunctionCmd(a).applyTo(currentProgram);
                    f = getFunctionAt(a);
                    created++;
                }
                if (f != null) f.setName(label, SourceType.USER_DEFINED);
                else st.createLabel(a, label, SourceType.USER_DEFINED);
            } else {
                st.createLabel(a, label, SourceType.USER_DEFINED).setPrimary();
            }
            n++;
        }
        println("applied " + n + " labels, created " + created + " functions");

        // "@0xADDR" entries: make sure a function exists there so it can be decompiled
        for (Pattern p : new ArrayList<>(wanted)) {
            String s = p.pattern();
            if (s.startsWith("in0x")) {
                // decompile the function CONTAINING this address
                Function f = getFunctionContaining(toAddr(Long.parseLong(s.substring(4), 16)));
                if (f != null) wanted.add(Pattern.compile(Pattern.quote(f.getName())));
                else println("no function contains " + s);
                continue;
            }
            if (s.startsWith("@0x")) {
                Address a = toAddr(Long.parseLong(s.substring(3), 16));
                if (getFunctionAt(a) == null) {
                    disassemble(a);
                    new CreateFunctionCmd(a).applyTo(currentProgram);
                }
                Function f = getFunctionAt(a);
                if (f == null) {
                    Function outer = getFunctionContaining(a);
                    if (outer != null) {
                        // split: shrink the enclosing function so the helper becomes its own function
                        removeFunction(outer);
                        new CreateFunctionCmd(a).applyTo(currentProgram);
                        new CreateFunctionCmd(outer.getEntryPoint()).applyTo(currentProgram);
                        f = getFunctionAt(a);
                    }
                }
                if (f != null) wanted.add(Pattern.compile(Pattern.quote(f.getName())));
                else println("could not create function at " + a);
            }
        }
        if (wanted.isEmpty()) return;
        DecompInterface di = new DecompInterface();
        DecompileOptions opts = new DecompileOptions();
        di.setOptions(opts);
        di.openProgram(currentProgram);
        for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
            String name = f.getName();
            boolean hit = false;
            for (Pattern p : wanted) if (p.matcher(name).matches()) { hit = true; break; }
            if (!hit) continue;
            DecompileResults r = di.decompileFunction(f, 600, monitor);
            String c = r.decompileCompleted() ? r.getDecompiledFunction().getC() : ("// FAILED: " + r.getErrorMessage());
            String header = "// " + name + " @ " + f.getEntryPoint() + "  size=" + f.getBody().getNumAddresses() + "\n";
            Files.writeString(outDir.resolve(name + ".c"), header + c);
            println("decompiled " + name);
        }
        di.dispose();
    }
}
