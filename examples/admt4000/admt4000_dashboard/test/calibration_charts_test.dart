// Copyright (C) 2026 Analog Devices, Inc.
//
// SPDX short identifier: ADIBSD
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:admt4000_dashboard/models/calibration_result.dart';
import 'package:admt4000_dashboard/widgets/calibration_charts.dart';

void main() {
  testWidgets('renders pre and post calibration charts without overflow', (
    tester,
  ) async {
    final results = [
      CalibrationPhaseResult(
        phase: 'pre',
        primaryAngles: List.generate(256, (i) => (i % 64) * 5.625),
        angleError: List.generate(256, (i) => 0.3 * (i % 7 - 3)),
        errorMag: List.generate(128, (i) => 0.1 / (i + 1)),
        correctedAngleError: List.generate(256, (i) => 0.05 * (i % 5 - 2)),
        correctedErrorMag: List.generate(128, (i) => 0.01 / (i + 1)),
      ),
      CalibrationPhaseResult(
        phase: 'post',
        primaryAngles: List.generate(256, (i) => (i % 64) * 5.625),
        angleError: List.generate(256, (i) => 0.05 * (i % 7 - 3)),
        errorMag: List.generate(128, (i) => 0.02 / (i + 1)),
      ),
    ];

    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SizedBox(
            height: 900,
            child: CalibrationCharts(results: results),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.text('Pre-Calibration'), findsOneWidget);
    expect(find.text('Corrected Error (degrees)'), findsOneWidget);

    await tester.dragUntilVisible(
      find.text('Post-Calibration'),
      find.byType(ListView),
      const Offset(0, -300),
    );
    expect(tester.takeException(), isNull);
    expect(find.text('Post-Calibration'), findsOneWidget);
  });
}
