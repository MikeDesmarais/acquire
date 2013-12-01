define(function(require) {
	var $ = require('jquery'),
		pubsub = require('pubsub');

	var moveElement = function($elements, left, top, width, height) {
			$elements.css('left', left);
			$elements.css('top', top);
			$elements.css('width', width);
			$elements.css('height', height);
		},
		resize = function() {
			var half_window_width = Math.floor($(window).width() / 2),
				$board = $('.board'),
				$score = $('.score'),
				cell_width = 0;

			cell_width = Math.floor((half_window_width - 2) / 12);
			moveElement($board, 0, 0, cell_width * 12 + 2, cell_width * 9 + 2);

			cell_width = Math.floor((half_window_width - 2) / 18);
			moveElement($score, half_window_width, 0, cell_width * 18 + 2, cell_width * 10 + 2);
			$score.find('tr').css('height', cell_width + 'px');
		},
		commandBoard = function(row, col, type) {
			var $cell = $('.board .row-' + row + ' .col-' + col);

			$cell.attr('class', 'col-' + col + ' ' + type);
			$cell.html('');
		};

	resize();
	$(window).resize(resize);

	pubsub.subscribe('board', commandBoard);

	return null;
});