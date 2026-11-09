package app.sleepwell.user;

import org.json.JSONArray;

import java.io.ByteArrayInputStream;
import java.io.DataInputStream;
import java.nio.charset.StandardCharsets;

/** Pure-Java inference runtime for the frozen SWLHGB01 mobile artifacts. */
final class HgbModel {
    private final double[] median, low, high, mean, scale;
    private final int[] indicator, keep;
    private final Estimator[] estimators;

    HgbModel(byte[] artifact) throws Exception {
        DataInputStream input = new DataInputStream(new ByteArrayInputStream(artifact));
        byte[] magic = new byte[8]; input.readFully(magic);
        if (!"SWLHGB01".equals(new String(magic, StandardCharsets.US_ASCII))) throw new IllegalArgumentException("bad model magic");
        median = readDoubles(input); indicator = readInts(input); low = readDoubles(input);
        high = readDoubles(input); keep = readInts(input); mean = readDoubles(input); scale = readDoubles(input);
        estimators = new Estimator[input.readInt()];
        for (int i = 0; i < estimators.length; i++) estimators[i] = new Estimator(input);
        if (input.available() != 0) throw new IllegalArgumentException("trailing model bytes=" + input.available());
    }

    double[] predict(double[] raw) {
        if (raw.length != median.length) throw new IllegalArgumentException("expected " + median.length + " features");
        double[] expanded = new double[raw.length + indicator.length];
        for (int i = 0; i < raw.length; i++) expanded[i] = Double.isNaN(raw[i]) ? median[i] : raw[i];
        for (int i = 0; i < indicator.length; i++) expanded[raw.length + i] = Double.isNaN(raw[indicator[i]]) ? 1.0 : 0.0;
        for (int i = 0; i < expanded.length; i++) expanded[i] = Math.min(high[i], Math.max(low[i], expanded[i]));
        double[] transformed = new double[keep.length];
        for (int i = 0; i < keep.length; i++) transformed[i] = (expanded[keep[i]] - mean[i]) / scale[i];
        double[] result = new double[estimators.length];
        for (int i = 0; i < estimators.length; i++) result[i] = estimators[i].predict(transformed);
        return result;
    }

    static double[] constrained(double[] raw) {
        double onset = Math.min(960.0, Math.max(0.0, raw[0]));
        double wake = Math.min(1440.0, raw[1]);
        wake = Math.max(onset + 30.0, Math.min(onset + 960.0, wake));
        return new double[]{onset, wake};
    }

    static double[] doubles(JSONArray source) throws Exception {
        double[] result = new double[source.length()];
        for (int i = 0; i < result.length; i++) result[i] = source.isNull(i) ? Double.NaN : source.getDouble(i);
        return result;
    }

    private static final class Estimator {
        final double baseline;
        final Tree[] trees;
        Estimator(DataInputStream input) throws Exception {
            baseline = input.readDouble();
            trees = new Tree[input.readInt()];
            for (int i = 0; i < trees.length; i++) trees[i] = new Tree(input);
        }
        double predict(double[] row) {
            double result = baseline;
            for (Tree tree : trees) result += tree.predict(row);
            return result;
        }
    }

    private static final class Tree {
        final int[] feature, left, right;
        final double[] threshold, leaf;
        final boolean[] missingLeft, isLeaf;
        Tree(DataInputStream input) throws Exception {
            int size = input.readInt();
            feature = new int[size]; left = new int[size]; right = new int[size];
            threshold = new double[size]; leaf = new double[size];
            missingLeft = new boolean[size]; isLeaf = new boolean[size];
            for (int i = 0; i < size; i++) {
                isLeaf[i] = input.readBoolean();
                if (isLeaf[i]) leaf[i] = input.readDouble();
                else {
                    feature[i] = input.readInt(); threshold[i] = input.readDouble();
                    missingLeft[i] = input.readBoolean();
                    left[i] = input.readInt(); right[i] = input.readInt();
                }
            }
        }
        double predict(double[] row) {
            int i = 0;
            while (!isLeaf[i]) {
                double value = row[feature[i]];
                boolean goLeft = Double.isNaN(value) ? missingLeft[i] : value <= threshold[i];
                i = goLeft ? left[i] : right[i];
            }
            return leaf[i];
        }
    }

    private static double[] readDoubles(DataInputStream input) throws Exception {
        double[] result = new double[input.readInt()];
        for (int i = 0; i < result.length; i++) result[i] = input.readDouble();
        return result;
    }

    private static int[] readInts(DataInputStream input) throws Exception {
        int[] result = new int[input.readInt()];
        for (int i = 0; i < result.length; i++) result[i] = input.readInt();
        return result;
    }
}
